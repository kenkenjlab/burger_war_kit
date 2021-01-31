#!/usr/bin/env python
# -*- coding: utf-8 -*-

from waypoint import Waypoints
from enemy_camera_detector import EnemyCameraDetector

from enum import Enum
import math
import os
import cv2
import numpy as np

import rospkg
import rospy
import tf
import actionlib


from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Twist
from sensor_msgs.msg import LaserScan, Image
from cv_bridge import CvBridge, CvBridgeError


class ActMode(Enum):
    BASIC = 1
    ATTACK = 2
    ESCAPE = 3
    DEFENCE = 4


class SeigoBot2:

    def __init__(self):

        def load_waypoint():
            current_dir=os.getcwd()
            print("current_dir", current_dir)
            rospack=rospkg.RosPack()
            self.WAYPOINT_CSV_FPATH=os.path.join(rospack.get_path("burger_war_level4"),"scripts","waypoints.csv")
            path = self.WAYPOINT_CSV_FPATH
            #path = os.environ['HOME'] + \
            #    '/catkin_ws/src/burger_war/enemy_bot/enemy_bot_level4/burger_war/scripts/waypoints.csv'
            return Waypoints(path)

        self.listener = tf.TransformListener()

        self.move_base_client = actionlib.SimpleActionClient(
            'move_base', MoveBaseAction)
        if not self.move_base_client.wait_for_server(rospy.Duration(5)):
            rospy.loginfo('wait move base server')
        rospy.loginfo('server comes up!!')
        self.status = self.move_base_client.get_state()

        rospy.Subscriber('enemy_position', Odometry,
                         self.enemy_position_callback)
        self.enemy_position = Odometry()
        self.enemy_info = [0.0, 0.0]
        self.detect_counter = 0

        rospy.Subscriber('scan', LaserScan, self.lidar_callback)
        self.scan = LaserScan()

        rospy.Subscriber('image_raw', Image, self.imageCallback)
        self.camera_detector = EnemyCameraDetector()
        self.is_camera_detect = False
        self.camera_detect_angle = -360

        self.direct_twist_pub = rospy.Publisher('cmd_vel', Twist, queue_size=1)

        self.act_mode = ActMode.BASIC
        self.get_rosparam()
        self.waypoint = load_waypoint()
        self.send_goal(self.waypoint.get_current_waypoint())

    def get_rosparam(self):
        self.robot_namespace = rospy.get_param('~robot_namespace')
        self.enemy_time_tolerance = rospy.get_param(
            'detect_enemy_time_tolerance', default=0.5)
        self.snipe_th = rospy.get_param('snipe_distance_th', default=0.8)
        self.distance_to_wall_th = rospy.get_param(
            'distance_to_wall_th', default=0.15)
        self.counter_th = rospy.get_param('enemy_count_th', default=3)
        self.approch_distance_th = rospy.get_param(
            'approch_distance_th', default=0.5)
        self.attack_angle_th = rospy.get_param(
            'attack_angle_th', default=45*math.pi/180)
        self.camera_range_limit = rospy.get_param(
            'camera_range_limit', default=[0.2, 0.5])
        self.camera_angle_limit = rospy.get_param(
            'camera_angle_limit', default=30)*math.pi/180

    def pi2pi(self, rad):
        mod = rad % 360
        if mod > 180:
            return -360+mod
        return mod

    def imageCallback(self, data):
        self.detect_from_camera(data)

    def get_position_from_tf(self, c1, c2):
        trans = []
        rot = []
        try:
            (trans, rot) = self.listener.lookupTransform(
                c1, c2, rospy.Time(0))
            return trans, rot, True
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            rospy.logwarn('tf error')
            return trans, rot, False

    def enemy_position_callback(self, position):
        self.enemy_position = position

    def lidar_callback(self, scan):
        self.scan = scan

    def detect_enemy(self):
        exist, distance, direction_diff = self.detect_from_lidar()
        # もしカメラで確認できる範囲なら

        if abs(direction_diff) < self.camera_angle_limit and distance > self.camera_range_limit[0] and distance < self.camera_range_limit[1]:
            exist = exist and self.is_camera_detect  # カメラとLidarのandをとる
            if exist == False:
                rospy.loginfo('detect enemy from LiDAR, but cannot detect from camera. So ignore')
        return exist, distance, direction_diff

    def detect_from_lidar(self):
        time_diff = rospy.Time.now().to_sec() - self.enemy_position.header.stamp.to_sec()
        if time_diff > self.enemy_time_tolerance:   # 敵情報が古かったら無視
            self.detect_counter = 0
            return False, 0.0, 0.0
        else:
            self.detect_counter = self.detect_counter+1
            if self.detect_counter < self.counter_th:
                return False, 0.0, 0.0

        map_topic = self.robot_namespace+"/map"
        baselink_topic= self.robot_namespace+"/base_link"
        trans, rot,  vaild = self.get_position_from_tf(map_topic, baselink_topic)
        if vaild == False:
            return False, 0.0, 0.0
        dx = self.enemy_position.pose.pose.position.x - trans[0]
        dy = self.enemy_position.pose.pose.position.y - trans[1]
        enemy_distance = math.sqrt(dx*dx+dy*dy)

        _, _, yaw = tf.transformations.euler_from_quaternion(rot)
        enemy_direction = math.atan2(dy, dx)
        enemy_direction_diff = self.pi2pi(enemy_direction-yaw)
        return True, enemy_distance, enemy_direction_diff

    def detect_from_camera(self, data):
        red_angle, green_angle, blue_angle = self.camera_detector.detect_enemy(
            data)
        if red_angle != -360:
            self.is_camera_detect = True
            self.camera_detect_angle = red_angle
            return
        else:
            if green_angle != -360:
                self.is_camera_detect = True
                self.camera_detect_angle = green_angle
            else:
                self.is_camera_detect = False
                self.camera_detect_angle = -360
        

    # RESPECT @koy_tak
    def detect_collision(self):
        front = False
        rear = False
        if (self.scan.ranges[0] != 0 and self.scan.ranges[0] < self.distance_to_wall_th) or (self.scan.ranges[10] != 0 and self.scan.ranges[10] < self.distance_to_wall_th) or (self.scan.ranges[350] != 0 and self.scan.ranges[350] < self.distance_to_wall_th):
            rospy.logwarn('front collision !!')
            front = True
        if (self.scan.ranges[180] != 0 and self.scan.ranges[180] < self.distance_to_wall_th) or (self.scan.ranges[190] != 0 and self.scan.ranges[190] < self.distance_to_wall_th) or (self.scan.ranges[170] != 0 and self.scan.ranges[170] < self.distance_to_wall_th):
            rospy.logwarn('rear collision !!')
            rear = True
        return front, rear

    # ここで状態決定　
    def mode_decision(self):
        exist, distance, direction_diff = self.detect_enemy()
        if exist == False:  # いなかったら巡回
            return ActMode.BASIC
        else:
            if distance < self.snipe_th:  # 発見して近かったら攻撃
                self.enemy_info = [distance, direction_diff]
                return ActMode.ATTACK
            rospy.loginfo('detect enemy but so far')
            return ActMode.BASIC

    def status_transition(self):
        def get_mode_txt(n):
            if n == ActMode.BASIC:
                return 'basic'
            elif n == ActMode.ATTACK:
                return 'attack'
            elif n == ActMode.ESCAPE:
                return 'escape'
            elif n == ActMode.DEFENCE:
                return 'defence'
            else:
                return 'unknown'

        pre_act_mode = self.act_mode
        self.act_mode = self.mode_decision()
        if self.act_mode == ActMode.BASIC:
            self.basic()
        elif self.act_mode == ActMode.ATTACK:
            self.attack()
        else:
            rospy.logwarn('unknown actmode !!!')

        if pre_act_mode != self.act_mode:
            text = 'change to ' + get_mode_txt(self.act_mode)
            rospy.loginfo(text)

    def basic(self):
        vaild, vx = self.recovery()  # ぶつかってないか確認
        if vaild == True:
            self.cancel_goal()
            cmd_vel = Twist()
            cmd_vel.linear.x = vx
            self.direct_twist_pub.publish(cmd_vel)
            return

        pre_status = self.status
        self.status = self.move_base_client.get_state()
        if pre_status != self.status:
            rospy.loginfo(self.move_base_client.get_goal_status_text())

        if self.status == actionlib.GoalStatus.ACTIVE:
            pass
        elif self.status == actionlib.GoalStatus.SUCCEEDED:
            point = self.waypoint.get_next_waypoint()
            self.send_goal(point)
        elif self.status == actionlib.GoalStatus.ABORTED:
            self.recovery()
        elif self.status == actionlib.GoalStatus.PENDING:
            self.send_goal(self.waypoint.get_current_waypoint())
        elif self.status == actionlib.GoalStatus.PREEMPTING or self.status == actionlib.GoalStatus.PREEMPTED:
            self.send_goal(self.waypoint.get_current_waypoint())
        else:
            return

    def send_goal(self, point):
        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = self.robot_namespace+"/map"
        goal.target_pose.pose.position.x = point[0]
        goal.target_pose.pose.position.y = point[1]

        q = tf.transformations.quaternion_from_euler(0, 0, point[2])
        goal.target_pose.pose.orientation.x = q[0]
        goal.target_pose.pose.orientation.y = q[1]
        goal.target_pose.pose.orientation.z = q[2]
        goal.target_pose.pose.orientation.w = q[3]
        goal.target_pose.header.stamp = rospy.Time.now()

        self.move_base_client.send_goal(goal)
        rospy.sleep(0.5)

        rospy.loginfo('send goal')

    def cancel_goal(self):
        self.move_base_client.cancel_all_goals()
        return

    def get_move_base_status(self):
        return self.move_base_client.get_state()

    def attack(self):
        self.cancel_goal()
        cmd_vel = self.turn_to_enemy(self.enemy_info[1])
        valid, vx = self.recovery()
        if valid == True:
            cmd_vel.linear.x = 0.0
        else:
            if abs(self.enemy_info[1]) < self.attack_angle_th:
                cmd_vel.linear.x = self.enemy_info[0]-self.approch_distance_th
            else:
                cmd_vel.linear.x = 0.0
        self.direct_twist_pub.publish(cmd_vel)

    def escape(self):
        return

    def defence(self):
        return

    def turn_to_enemy(self, direction_diff):
        cmd_vel = Twist()
        cmd_vel.angular.z = direction_diff
        return cmd_vel

    def recovery(self):
        front, rear = self.detect_collision()
        if front == True:
            return True, -0.1
        elif rear == True:
            return True, 0.1
        else:
            return False, 0.0


def main():
    rospy.init_node('seigo_run2')
    node = SeigoBot2()
    rate = rospy.Rate(30)
    while not rospy.is_shutdown():
        node.status_transition()
        rate.sleep()


if __name__ == "__main__":
    main()
