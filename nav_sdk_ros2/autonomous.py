# Copyright (c) 2023 Boston Dynamics, Inc.  All rights reserved.
#
# Downloading, reproducing, distributing or otherwise using the SDK Software
# is subject to the terms and conditions of the Boston Dynamics Software
# Development Kit License (20191101-BDSDK-SL).

"""Command line interface for graph nav with options to download/upload a map and to navigate a map. """

import argparse
import logging
import math
import os
import sys
import time

import rclpy
from rclpy.node import Node
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from tf_transformations import euler_from_quaternion

from rclpy.action import ActionServer
from rclpy.action import CancelResponse
from rclpy.action import GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.duration import Duration
from rclpy.qos import QoSProfile
from create_message_types.action import Navigation

import random
from bosdyn.client import spot_cam

import argparse

import google.protobuf.timestamp_pb2
from .submodules.graph_nav_util import *
import grpc

import bosdyn.client.channel
import bosdyn.client.util
from bosdyn.api import geometry_pb2, power_pb2, robot_state_pb2
from bosdyn.api.graph_nav import graph_nav_pb2, map_pb2, nav_pb2
from bosdyn.client.exceptions import ResponseError
from bosdyn.client.frame_helpers import get_odom_tform_body
from bosdyn.client.graph_nav import GraphNavClient
from bosdyn.client.lease import LeaseClient, LeaseKeepAlive, ResourceAlreadyClaimedError
from bosdyn.client.math_helpers import Quat, SE3Pose
from bosdyn.client.power import PowerClient, power_on_motors, safe_power_off_motors
from bosdyn.client.robot_command import RobotCommandBuilder, RobotCommandClient
from bosdyn.client.robot_state import RobotStateClient


class GraphNavInterface(object):
    """GraphNav service command line interface."""

    def __init__(self, robot, upload_path):
        self._robot = robot

        # Robot state
        self._robot_state_x = 0
        self._robot_state_y = 0
        self._robot_state_yaw = 0

        self._flag_nav = False

        # Force trigger timesync.
        self._robot.time_sync.wait_for_sync()

        # Create robot state and command clients.
        self._robot_command_client = self._robot.ensure_client(
            RobotCommandClient.default_service_name)
        self._robot_state_client = self._robot.ensure_client(RobotStateClient.default_service_name)

        # Create the client for the Graph Nav main service.
        self._graph_nav_client = self._robot.ensure_client(GraphNavClient.default_service_name)

        # Create a power client for the robot.
        self._power_client = self._robot.ensure_client(PowerClient.default_service_name)

        # Boolean indicating the robot's power state.
        power_state = self._robot_state_client.get_robot_state().power_state
        self._started_powered_on = (power_state.motor_power_state == power_state.STATE_ON)
        self._powered_on = self._started_powered_on

        # Number of attempts to wait before trying to re-power on.
        self._max_attempts_to_wait = 50

        # Store the most recent knowledge of the state of the robot based on rpc calls.
        self._current_graph = None
        self._current_edges = dict()  #maps to_waypoint to list(from_waypoint)
        self._current_waypoint_snapshots = dict()  # maps id to waypoint snapshot
        self._current_edge_snapshots = dict()  # maps id to edge snapshot
        self._current_annotation_name_to_wp_id = dict()

        # Filepath for uploading a saved graph's and snapshots too.
        if upload_path[-1] == '/':
            self._upload_filepath = upload_path[:-1]
        else:
            self._upload_filepath = upload_path

        self._command_dictionary = {
            '1': self._get_localization_state,
            '2': self._set_initial_localization_fiducial,
            '3': self._set_initial_localization_waypoint,
            '4': self._list_graph_waypoint_and_edge_ids,
            '5': self._upload_graph_and_snapshots,
            '6': self._navigate_to,
            '7': self._navigate_route,
            '8': self._navigate_to_anchor,
            '9': self._clear_graph,
            '10': self._navigate_to_anchor_once
        }

    def _get_localization_state(self, *args):
        """Get the current localization and state of the robot."""
        state = self._graph_nav_client.get_localization_state()
        print(f'Got localization: \n{state.localization}')
        odom_tform_body = get_odom_tform_body(state.robot_kinematics.transforms_snapshot)
        print(f'Got robot state in kinematic odometry frame: \n{odom_tform_body}')

    def _set_initial_localization_fiducial(self, *args):
        """Trigger localization when near a fiducial."""
        robot_state = self._robot_state_client.get_robot_state()
        current_odom_tform_body = get_odom_tform_body(
            robot_state.kinematic_state.transforms_snapshot).to_proto()
        # Create an empty instance for initial localization since we are asking it to localize
        # based on the nearest fiducial.
        localization = nav_pb2.Localization()
        self._graph_nav_client.set_localization(initial_guess_localization=localization,
                                                ko_tform_body=current_odom_tform_body)


    def _set_initial_localization_waypoint(self, *args):
        """Trigger localization to a waypoint."""
        # Take the first argument as the localization waypoint.
        if len(args) < 1:
            # If no waypoint id is given as input, then return without initializing.
            print('No waypoint specified to initialize to.')
            return
        destination_waypoint = find_unique_waypoint_id(
            args[0][0], self._current_graph, self._current_annotation_name_to_wp_id)
        if not destination_waypoint:
            # Failed to find the unique waypoint id.
            return

        robot_state = self._robot_state_client.get_robot_state()
        current_odom_tform_body = get_odom_tform_body(
            robot_state.kinematic_state.transforms_snapshot).to_proto()
        # Create an initial localization to the specified waypoint as the identity.
        localization = nav_pb2.Localization()
        localization.waypoint_id = destination_waypoint
        localization.waypoint_tform_body.rotation.w = 1.0
        self._graph_nav_client.set_localization(
            initial_guess_localization=localization,
            # It's hard to get the pose perfect, search +/-20 deg and +/-20cm (0.2m).
            max_distance=0.2,
            max_yaw=20.0 * math.pi / 180.0,
            fiducial_init=graph_nav_pb2.SetLocalizationRequest.FIDUCIAL_INIT_NO_FIDUCIAL,
            ko_tform_body=current_odom_tform_body)

    def _list_graph_waypoint_and_edge_ids(self, *args):
        """List the waypoint ids and edge ids of the graph currently on the robot."""

        # Download current graph
        graph = self._graph_nav_client.download_graph()
        if graph is None:
            print('Empty graph.')
            return
        self._current_graph = graph

        localization_id = self._graph_nav_client.get_localization_state().localization.waypoint_id

        # Update and print waypoints and edges
        self._current_annotation_name_to_wp_id, self._current_edges = update_waypoints_and_edges(
            graph, localization_id)

    def _upload_graph_and_snapshots(self, *args):
        """Upload the graph and snapshots to the robot."""
        print('Loading the graph from disk into local storage...')
        with open(self._upload_filepath + '/graph', 'rb') as graph_file:
            # Load the graph from disk.
            data = graph_file.read()
            self._current_graph = map_pb2.Graph()
            self._current_graph.ParseFromString(data)
            print(
                f'Loaded graph has {len(self._current_graph.waypoints)} waypoints and {self._current_graph.edges} edges'
            )
        for waypoint in self._current_graph.waypoints:
            # Load the waypoint snapshots from disk.
            with open(f'{self._upload_filepath}/waypoint_snapshots/{waypoint.snapshot_id}',
                      'rb') as snapshot_file:
                waypoint_snapshot = map_pb2.WaypointSnapshot()
                waypoint_snapshot.ParseFromString(snapshot_file.read())
                self._current_waypoint_snapshots[waypoint_snapshot.id] = waypoint_snapshot
        for edge in self._current_graph.edges:
            if len(edge.snapshot_id) == 0:
                continue
            # Load the edge snapshots from disk.
            with open(f'{self._upload_filepath}/edge_snapshots/{edge.snapshot_id}',
                      'rb') as snapshot_file:
                edge_snapshot = map_pb2.EdgeSnapshot()
                edge_snapshot.ParseFromString(snapshot_file.read())
                self._current_edge_snapshots[edge_snapshot.id] = edge_snapshot
        # Upload the graph to the robot.
        print('Uploading the graph and snapshots to the robot...')
        true_if_empty = not len(self._current_graph.anchoring.anchors)
        response = self._graph_nav_client.upload_graph(graph=self._current_graph,
                                                       generate_new_anchoring=true_if_empty)
        # Upload the snapshots to the robot.
        for snapshot_id in response.unknown_waypoint_snapshot_ids:
            waypoint_snapshot = self._current_waypoint_snapshots[snapshot_id]
            self._graph_nav_client.upload_waypoint_snapshot(waypoint_snapshot)
            print(f'Uploaded {waypoint_snapshot.id}')
        for snapshot_id in response.unknown_edge_snapshot_ids:
            edge_snapshot = self._current_edge_snapshots[snapshot_id]
            self._graph_nav_client.upload_edge_snapshot(edge_snapshot)
            print(f'Uploaded {edge_snapshot.id}')

        # The upload is complete! Check that the robot is localized to the graph,
        # and if it is not, prompt the user to localize the robot before attempting
        # any navigation commands.
        localization_state = self._graph_nav_client.get_localization_state()
        if not localization_state.localization.waypoint_id:
            # The robot is not localized to the newly uploaded graph.
            print('\n')
            print(
                'Upload complete! The robot is currently not localized to the map; please localize'
                ' the robot using commands (2) or (3) before attempting a navigation command.')

    def _navigate_to_anchor_once(self, *args):
        """Navigate to a pose in seed frame, using anchors."""
        # The following options are accepted for arguments: [x, y], [x, y, yaw], [x, y, z, yaw],
        # [x, y, z, qw, qx, qy, qz].
        # When a value for z is not specified, we use the current z height.
        # When only yaw is specified, the quaternion is constructed from the yaw.
        # When yaw is not specified, an identity quaternion is used.

        if self._flag_nav == False:
            if len(args) < 1 or len(args[0]) not in [2, 3, 4, 7]:
                print('Invalid arguments supplied.')
                return

            self._seed_T_goal = SE3Pose(float(args[0][0]), float(args[0][1]), 0.0, Quat())

            if len(args[0]) in [4, 7]:
                self._seed_T_goal.z = float(args[0][2])
            else:
                localization_state = self._graph_nav_client.get_localization_state()
                if not localization_state.localization.waypoint_id:
                    print('Robot not localized')
                    return
                self._seed_T_goal.z = localization_state.localization.seed_tform_body.position.z

            if len(args[0]) == 3:
                self._seed_T_goal.rot = Quat.from_yaw(float(args[0][2]))
            elif len(args[0]) == 4:
                self._seed_T_goal.rot = Quat.from_yaw(float(args[0][3]))
            elif len(args[0]) == 7:
                self._seed_T_goal.rot = Quat(w=float(args[0][3]), x=float(args[0][4]), y=float(args[0][5]),
                                       z=float(args[0][6]))

            if not self.toggle_power(should_power_on=True):
                print('Failed to power on the robot, and cannot complete navigate to request.')
                return

        nav_to_cmd_id = None
        # Navigate to the destination.
        is_finished = False
        
        self._flag_nav = True

        # Issue the navigation command about twice a second such that it is easy to terminate the
        # navigation command (with estop or killing the program).
        state = self._graph_nav_client.get_localization_state() 
        self._robot_state_x = state.localization.seed_tform_body.position.x
        self._robot_state_y = state.localization.seed_tform_body.position.y
        rot = state.localization.seed_tform_body.rotation
        orientation = [rot.x, rot.y, rot.z, rot.w]
        (roll, pitch, yaw) = euler_from_quaternion(orientation)
        self._robot_state_yaw = yaw
  
        nav_to_cmd_id = self._graph_nav_client.navigate_to_anchor(
            self._seed_T_goal.to_proto(), 1.0, command_id=nav_to_cmd_id)
        
        time.sleep(.5)  # Sleep for half a second to allow for command execution.
        # Poll the robot for feedback to determine if the navigation command is complete. Then sit
        # the robot down once it is finished.
        is_finished = self._check_success(nav_to_cmd_id)
 
        print(is_finished)

        if is_finished == True:
            self._flag_nav = False

        return is_finished


    def _navigate_to_anchor(self, *args):
        """Navigate to a pose in seed frame, using anchors."""
        # The following options are accepted for arguments: [x, y], [x, y, yaw], [x, y, z, yaw],
        # [x, y, z, qw, qx, qy, qz].
        # When a value for z is not specified, we use the current z height.
        # When only yaw is specified, the quaternion is constructed from the yaw.
        # When yaw is not specified, an identity quaternion is used.

        if len(args) < 1 or len(args[0]) not in [2, 3, 4, 7]:
            print('Invalid arguments supplied.')
            return

        seed_T_goal = SE3Pose(float(args[0][0]), float(args[0][1]), 0.0, Quat())

        if len(args[0]) in [4, 7]:
            seed_T_goal.z = float(args[0][2])
        else:
            localization_state = self._graph_nav_client.get_localization_state()
            if not localization_state.localization.waypoint_id:
                print('Robot not localized')
                return
            seed_T_goal.z = localization_state.localization.seed_tform_body.position.z

        if len(args[0]) == 3:
            seed_T_goal.rot = Quat.from_yaw(float(args[0][2]))
        elif len(args[0]) == 4:
            seed_T_goal.rot = Quat.from_yaw(float(args[0][3]))
        elif len(args[0]) == 7:
            seed_T_goal.rot = Quat(w=float(args[0][3]), x=float(args[0][4]), y=float(args[0][5]),
                                   z=float(args[0][6]))

        if not self.toggle_power(should_power_on=True):
            print('Failed to power on the robot, and cannot complete navigate to request.')
            return

        nav_to_cmd_id = None
        # Navigate to the destination.
        is_finished = False
        while not is_finished:
            # Issue the navigation command about twice a second such that it is easy to terminate the
            # navigation command (with estop or killing the program).
            try:
                state = self._graph_nav_client.get_localization_state() 
                self._robot_state_x = state.localization.seed_tform_body.position.x
                self._robot_state_y = state.localization.seed_tform_body.position.y
                rot = state.localization.seed_tform_body.rotation
                orientation = [rot.x, rot.y, rot.z, rot.w]
                (roll, pitch, yaw) = euler_from_quaternion(orientation)
                self._robot_state_yaw = yaw 
                print(f'X: {self._robot_state_x}  Y: {self._robot_state_y}  YAW: {self._robot_state_yaw}')

                nav_to_cmd_id = self._graph_nav_client.navigate_to_anchor(
                    seed_T_goal.to_proto(), 1.0, command_id=nav_to_cmd_id)
            except ResponseError as e:
                print(f'Error while navigating {e}')
                break
            time.sleep(.5)  # Sleep for half a second to allow for command execution.
            # Poll the robot for feedback to determine if the navigation command is complete. Then sit
            # the robot down once it is finished.
            is_finished = self._check_success(nav_to_cmd_id)

        # Power off the robot if appropriate.
        #if self._powered_on and not self._started_powered_on:
        #    # Sit the robot down + power off after the navigation command is complete.
        #    self.toggle_power(should_power_on=False)

    def _navigate_to(self, *args):
        """Navigate to a specific waypoint."""
        # Take the first argument as the destination waypoint.
        if len(args) < 1:
            # If no waypoint id is given as input, then return without requesting navigation.
            print('No waypoint provided as a destination for navigate to.')
            return

        destination_waypoint = find_unique_waypoint_id(
            args[0][0], self._current_graph, self._current_annotation_name_to_wp_id)
        if not destination_waypoint:
            # Failed to find the appropriate unique waypoint id for the navigation command.
            return
        if not self.toggle_power(should_power_on=True):
            print('Failed to power on the robot, and cannot complete navigate to request.')
            return

        nav_to_cmd_id = None
        # Navigate to the destination waypoint.
        is_finished = False
        while not is_finished:
            # Issue the navigation command about twice a second such that it is easy to terminate the
            # navigation command (with estop or killing the program).
            try:
                state = self._graph_nav_client.get_localization_state() ################################
                print(f'Got localization: \n{state.localization}')      ################################

                nav_to_cmd_id = self._graph_nav_client.navigate_to(destination_waypoint, 1.0,
                                                                   command_id=nav_to_cmd_id)
            except ResponseError as e:
                print(f'Error while navigating {e}')
                break
            time.sleep(.5)  # Sleep for half a second to allow for command execution.
            # Poll the robot for feedback to determine if the navigation command is complete. Then sit
            # the robot down once it is finished.
            is_finished = self._check_success(nav_to_cmd_id)

        # Power off the robot if appropriate.
        #if self._powered_on and not self._started_powered_on:
        #    # Sit the robot down + power off after the navigation command is complete.
        #    self.toggle_power(should_power_on=False)

    def _navigate_route(self, *args):
        """Navigate through a specific route of waypoints."""
        if len(args) < 1 or len(args[0]) < 1:
            # If no waypoint ids are given as input, then return without requesting navigation.
            print('No waypoints provided for navigate route.')
            return
        waypoint_ids = args[0]
        for i in range(len(waypoint_ids)):
            waypoint_ids[i] = find_unique_waypoint_id(
                waypoint_ids[i], self._current_graph, self._current_annotation_name_to_wp_id)
            if not waypoint_ids[i]:
                # Failed to find the unique waypoint id.
                return

        edge_ids_list = []
        all_edges_found = True
        # Attempt to find edges in the current graph that match the ordered waypoint pairs.
        # These are necessary to create a valid route.
        for i in range(len(waypoint_ids) - 1):
            start_wp = waypoint_ids[i]
            end_wp = waypoint_ids[i + 1]
            edge_id = self._match_edge(self._current_edges, start_wp, end_wp)
            if edge_id is not None:
                edge_ids_list.append(edge_id)
            else:
                all_edges_found = False
                print(f'Failed to find an edge between waypoints: {start_wp} and {end_wp}')
                print(
                    'List the graph\'s waypoints and edges to ensure pairs of waypoints has an edge.'
                )
                break

        if all_edges_found:
            if not self.toggle_power(should_power_on=True):
                print('Failed to power on the robot, and cannot complete navigate route request.')
                return

            # Navigate a specific route.
            route = self._graph_nav_client.build_route(waypoint_ids, edge_ids_list)
            is_finished = False
            while not is_finished:
                # Issue the route command about twice a second such that it is easy to terminate the
                # navigation command (with estop or killing the program).
                nav_route_command_id = self._graph_nav_client.navigate_route(
                    route, cmd_duration=1.0)
                time.sleep(.5)  # Sleep for half a second to allow for command execution.
                # Poll the robot for feedback to determine if the route is complete. Then sit
                # the robot down once it is finished.
                is_finished = self._check_success(nav_route_command_id)

            # Power off the robot if appropriate.
            if self._powered_on and not self._started_powered_on:
                # Sit the robot down + power off after the navigation command is complete.
                self.toggle_power(should_power_on=False)

    def _clear_graph(self, *args):
        """Clear the state of the map on the robot, removing all waypoints and edges."""
        return self._graph_nav_client.clear_graph()

    def toggle_power(self, should_power_on):
        """Power the robot on/off dependent on the current power state."""
        is_powered_on = self.check_is_powered_on()
        if not is_powered_on and should_power_on:
            # Power on the robot up before navigating when it is in a powered-off state.
            power_on_motors(self._power_client)
            motors_on = False
            while not motors_on:
                future = self._robot_state_client.get_robot_state_async()
                state_response = future.result(
                    timeout=10)  # 10 second timeout for waiting for the state response.
                if state_response.power_state.motor_power_state == robot_state_pb2.PowerState.STATE_ON:
                    motors_on = True
                else:
                    # Motors are not yet fully powered on.
                    time.sleep(.25)
        elif is_powered_on and not should_power_on:
            # Safe power off (robot will sit then power down) when it is in a
            # powered-on state.
            safe_power_off_motors(self._robot_command_client, self._robot_state_client)
        else:
            # Return the current power state without change.
            return is_powered_on
        # Update the locally stored power state.
        self.check_is_powered_on()
        return self._powered_on

    def check_is_powered_on(self):
        """Determine if the robot is powered on or off."""
        power_state = self._robot_state_client.get_robot_state().power_state
        self._powered_on = (power_state.motor_power_state == power_state.STATE_ON)
        return self._powered_on

    def _check_success(self, command_id=-1):
        """Use a navigation command id to get feedback from the robot and sit when command succeeds."""
        if command_id == -1:
            # No command, so we have no status to check.
            return False
        status = self._graph_nav_client.navigation_feedback(command_id)
        if status.status == graph_nav_pb2.NavigationFeedbackResponse.STATUS_REACHED_GOAL:
            # Successfully completed the navigation commands!
            return True
        elif status.status == graph_nav_pb2.NavigationFeedbackResponse.STATUS_LOST:
            print('Robot got lost when navigating the route, the robot will now sit down.')
            return True
        elif status.status == graph_nav_pb2.NavigationFeedbackResponse.STATUS_STUCK:
            print('Robot got stuck when navigating the route, the robot will now sit down.')
            return True
        elif status.status == graph_nav_pb2.NavigationFeedbackResponse.STATUS_ROBOT_IMPAIRED:
            print('Robot is impaired.')
            return True
        else:
            # Navigation command is not complete yet.
            return False

    def _match_edge(self, current_edges, waypoint1, waypoint2):
        """Find an edge in the graph that is between two waypoint ids."""
        # Return the correct edge id as soon as it's found.
        for edge_to_id in current_edges:
            for edge_from_id in current_edges[edge_to_id]:
                if (waypoint1 == edge_to_id) and (waypoint2 == edge_from_id):
                    # This edge matches the pair of waypoints! Add it the edge list and continue.
                    return map_pb2.Edge.Id(from_waypoint=waypoint2, to_waypoint=waypoint1)
                elif (waypoint2 == edge_to_id) and (waypoint1 == edge_from_id):
                    # This edge matches the pair of waypoints! Add it the edge list and continue.
                    return map_pb2.Edge.Id(from_waypoint=waypoint1, to_waypoint=waypoint2)
        return None

    def _on_quit(self):
        """Cleanup on quit from the command line interface."""
        # Sit the robot down + power off after the navigation command is complete.
        if self._powered_on and not self._started_powered_on:
            self._robot_command_client.robot_command(RobotCommandBuilder.safe_power_off_command(),
                                                     end_time_secs=time.time())

    def run(self,total_number_waypoints):
        # Upload Graph
        try:
            cmd_func = self._command_dictionary['5']
            cmd_func()
        except Exception as e:
            print(e)
       
        # Get list of waypoint
        try:
            cmd_func = self._command_dictionary['4']
            cmd_func()
        except Exception as e:
            print(e)

        print(self._current_annotation_name_to_wp_id)

        number_waypoints = 0
        while number_waypoints < total_number_waypoints:
            # Select a random waypoint
            res = key, val = random.choice(list(self._current_annotation_name_to_wp_id.items()))
            cmd_func = self._command_dictionary['6']
            
            print("Heading to the next waypoint: "+val)
            
            # Move to the next waypoint
            cmd_func(str.split(val))
            
            # Light on
            
            # Sleep
            time.sleep(5)

            # Light off

            # Next waypoint
            number_waypoints+=1

        # Move to its initial position
        cmd_func = self._command_dictionary['8']
        cmd_func([1.67,0,3.1])       

        # Power off the robot if appropriate.
        if self._powered_on and not self._started_powered_on:
            # Sit the robot down + power off after the navigation command is complete.
            self.toggle_power(should_power_on=False)
  
    def run_next_position(self, x, y, yaw):
        # Upload Graph
        try:
            cmd_func = self._command_dictionary['5']
            cmd_func()
        except Exception as e:
            print(e)
        
        # Move to its initial position
        cmd_func = self._command_dictionary['8']
        cmd_func([x,y,yaw])       
  
    def run_next_position_once(self, x, y, yaw):
       # Move to its initial position
       cmd_func = self._command_dictionary['10']
       result = cmd_func([x,y,yaw])
       return result
     

class NavROS2SDK(Node):
    def __init__(self, graph_nav_command_line, lease_client):
        super().__init__("Nav_wrapper")
        self.get_logger().info("This nav node works between ros2 and the spot sdk.")
        
        # SDK
        self.graph_nav_command_line = graph_nav_command_line
        self.lease_client = lease_client

        # Action message
        self.nav = Navigation.Goal()
 
        # Goal
        self.goal_x = 0
        self.goal_y = 0
        self.goal_yaw = 0

        # Initialise servers
        self.action_server = ActionServer(
            self,
            Navigation,
            'navigation',
            execute_callback=self.execute_callback,
            callback_group=ReentrantCallbackGroup(),
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback)

    def goal_callback(self, goal_request):
        # Accepts or rejects a client request to begin an action
        self.get_logger().info('Received goal request :)')
        self.goal_x = goal_request.x
        self.goal_y = goal_request.y
        self.goal_yaw = goal_request.yaw
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        # Accepts or rejects a client request to cancel an action
        self.get_logger().info('Received cancel request :(')
        return CancelResponse.ACCEPT

    async def execute_callback(self, goal_handle):
        self.get_logger().info('Executing goal...')
        
        try:
            with LeaseKeepAlive(self.lease_client, must_acquire=True, return_at_exit=True):
                while rclpy.ok():
                    if goal_handle.is_cancel_requested:
                        goal_handle.canceled()
                        self.get_logger().info('Goal canceled')
                        return Navigation.Result()
                    try:
                        # Send command
                        #print(f"Send command: i: {i}")
                        result = self.graph_nav_command_line.run_next_position_once(self.goal_x, self.goal_y, self.goal_yaw)
                        print(f'Is it Completed: {result}')

                        # Get Robot position
                        print(f'X: {self.graph_nav_command_line._robot_state_x}  Y: {self.graph_nav_command_line._robot_state_y}  YAW: {self.graph_nav_command_line._robot_state_yaw}')
                        feedback_msg = Navigation.Feedback()
                        feedback_msg.x = self.graph_nav_command_line._robot_state_x
                        feedback_msg.y = self.graph_nav_command_line._robot_state_y
                        feedback_msg.yaw = self.graph_nav_command_line._robot_state_yaw
                        goal_handle.publish_feedback(feedback_msg)

                        if result:
                            goal_handle.succeed()
                            result = Navigation.Result()
                            result.success = True
                            return result

                    except Exception as exc:  # pylint: disable=broad-except
                        print(exc)
                        print('Graph nav command line client threw an error.')
                        result = Navigation.Result()
                        result.success = False
                        return result

        except ResourceAlreadyClaimedError:
            print('The robot\'s lease is currently in use. Check for a tablet connection or try again in a few seconds.')
            result = Navigation.Result()
            result.success = False
            return result

    def run(self):
        try:
            with LeaseKeepAlive(self.lease_client, must_acquire=True, return_at_exit=True):
                # Upload Graph
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['5']
                    cmd_func()
                except Exception as e:
                    print(e)
                    return False

                # List waypoints
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['4']
                    cmd_func()
                except Exception as e:
                    print(e)
                    return False

                # Initialize localization to the nearest fiducial (must be in sight of a fiducial).
                #try:
                #    cmd_func = self.graph_nav_command_line._command_dictionary['2']
                #    cmd_func()
                #except Exception as e:
                #    print(e)
                #    result = navigation.Result()
                #    result.success = False
                #    return result

                # Initialize localization to a specific waypoint (must be exactly at the waypoint).
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['3']
                    cmd_func(['rp'])
                except Exception as e:
                    print(e)
                    return False
        except ResourceAlreadyClaimedError:
            print('The robot\'s lease is currently in use. Check for a tablet connection or try again in a few seconds.')
            return False

        print("Ready!")

        while rclpy.ok():
            rclpy.spin_once(self)

    def run_temp(self):
        try:
            with LeaseKeepAlive(self.lease_client, must_acquire=True, return_at_exit=True):
                # Upload Graph
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['5']
                    cmd_func()
                except Exception as e:
                    print(e)
                    return False

                # List waypoints
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['4']
                    cmd_func()
                except Exception as e:
                    print(e)
                    return False

                # Initialize localization to the nearest fiducial (must be in sight of a fiducial).
                #try:
                #    cmd_func = self.graph_nav_command_line._command_dictionary['2']
                #    cmd_func()
                #except Exception as e:
                #    print(e)
                #    return False

                # Initialize localization to a specific waypoint (must be exactly at the waypoint).
                try:
                    cmd_func = self.graph_nav_command_line._command_dictionary['3']
                    cmd_func(['rp'])
                except Exception as e:
                    print(e)
                    return False

                destinations = [(2.5,-1.0,1.7),(2.0,-1.5,0),(1.67,0,3.1)]
                i = 0
                while rclpy.ok():
                    #print(":: Initiate Loop")
                    #rclpy.spin_once(self)
                    try:    
                        # Send command
                        #print(f"Send command: i: {i}")
                        result = self.graph_nav_command_line.run_next_position_once(destinations[i][0],destinations[i][1],destinations[i][2])
                        print(f'Is it Completed: {result}')

                        # Get Robot position
                        print(f'X: {self.graph_nav_command_line._robot_state_x}  Y: {self.graph_nav_command_line._robot_state_y}  YAW: {self.graph_nav_command_line._robot_state_yaw}')

                        if result:       
                            i = i + 1
                            if i == len(destinations):
                                return True

                    except Exception as exc:  # pylint: disable=broad-except
                        print(exc)
                        print('Graph nav command line client threw an error.')
                        return False
        except ResourceAlreadyClaimedError:
            print('The robot\'s lease is currently in use. Check for a tablet connection or try again in a few seconds.')
            return False


    def run_path(self):
        try:
            with LeaseKeepAlive(self.lease_client, must_acquire=True, return_at_exit=True):
                try:
                    # Send number of random waypoints
                    #graph_nav_command_line.run(3)
                   
                    # Send a sequence of positions
                    self.graph_nav_command_line.run_next_position(1.67,0,3.1)
                    self.graph_nav_command_line.run_next_position(2.0,-2.0,0)
                    self.graph_nav_command_line.run_next_position(1.67,0,3.1)

                    return True
                except Exception as exc:  # pylint: disable=broad-except
                    print(exc)
                    print('Graph nav command line client threw an error.')
                    return False
        except ResourceAlreadyClaimedError:
            print(
                'The robot\'s lease is currently in use. Check for a tablet connection or try again in a few seconds.'
            )
            return False



def main(argv=None):
    """Run the command-line interface."""
    parser = argparse.ArgumentParser(description=__doc__)
    bosdyn.client.util.add_base_arguments(parser)

    # Setup and authenticate the robot.
    sdk = bosdyn.client.create_standard_sdk('GraphNavSDKROS2')
    spot_cam.register_all_service_clients(sdk)
    hostname = "192.168.80.3"
    upload_filepath = "/root/ros2_ws/src/nav_sdk_ros2/nav_sdk_ros2/downloaded_graph"
    robot = sdk.create_robot(hostname)
    bosdyn.client.util.authenticate(robot)

    graph_nav_command_line = GraphNavInterface(robot, upload_filepath)
    lease_client = robot.ensure_client(LeaseClient.default_service_name)

    # ROS2
    rclpy.init()
    node = NavROS2SDK(graph_nav_command_line, lease_client)
    
    # ROS2 - Multithread
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    
    # Loop
    node.run()
    
    # Kill and shutdown
    node.destroy_node()
    rclpy.shutdown()

    '''
    hostname = "192.168.80.3"
    upload_filepath = "/root/ros2_ws/src/nav_sdk_ros2/nav_sdk_ros2/downloaded_graph"
    robot = sdk.create_robot(hostname)
    bosdyn.client.util.authenticate(robot)

    graph_nav_command_line = GraphNavInterface(robot, upload_filepath)
    lease_client = robot.ensure_client(LeaseClient.default_service_name)
    try:
        with LeaseKeepAlive(lease_client, must_acquire=True, return_at_exit=True):
            try:
                graph_nav_command_line.run()
                return True
            except Exception as exc:  # pylint: disable=broad-except
                print(exc)
                print('Graph nav command line client threw an error.')
                return False
    except ResourceAlreadyClaimedError:
        print(
            'The robot\'s lease is currently in use. Check for a tablet connection or try again in a few seconds.'
        )
        return False
    '''

if __name__ == '__main__':
    main()
