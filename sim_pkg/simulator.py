#!/usr/bin/env python3
""" Main Server for Swarm Simulation """

import random
import socket
import sys
import json
import select
import time
import re
import numpy as np
from itertools import chain
import signal
import functiontrace
import os
import logging
import argparse

from sim_pkg.config import Configuration

# TODO: Remove this hack, should be scoped in a function
parser = argparse.ArgumentParser()
parser.add_argument('-c', '--config', type=str, required=True)
args = parser.parse_args()
config = Configuration.from_path(os.path.abspath(args.config))

server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.bind((socket.gethostname(), config.server_port))
server_socket.listen(1)
open_client_sockets = [] # current clients handler
messages_to_send = [] # future message send handler
elapsedDIffList = []

class GracefulKiller:
    """
    Checks whether a sigint or sigterm signal is received. If so, it will help 
    exit the main while loop
    """
    kill_now = False
    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, *args):
        self.kill_now = True

class BotDiffDrive:
    """
    Bot Diff Drive robot with all the math
    """
    def __init__(self, id_=0, pos_x_=0.0, pos_y_=0.0, pos_angle_=0.0, clk_=0, usr_led_=(0,0,0)):
        """
        Define base variables. 
        All values are expected in metres, seconds and radians. 
        """
        self.id = id_
        self.pos_x = pos_x_
        self.pos_y = pos_y_
        self.pos_angle = pos_angle_
        self.left_wheel_angle = 0
        self.right_wheel_angle = 0
        self.radius_of_wheel = 0.015
        self.distance_between_wheel = 0.04
        self.clk = clk_
        self.usr_led = usr_led_
        

    def integrate(self,u_left, u_right, delta_time):
        """
        Integrate the state of robot
        """
        velocity_vector = np.array([[u_left],[u_right]])
        pos = self.dynamics(velocity_vector*delta_time)
        pos[0] = (pos[0]+ np.pi) % (2 * np.pi) - np.pi
        return pos
        
    def dynamics(self, vel_vector):
        """
        State dynamic model used to integrate using euler integration
        """
        pos_angle_ = self.pos_angle
        state_matrix = np.array([[-self.radius_of_wheel/(2.0*self.distance_between_wheel), self.radius_of_wheel/(2.0*self.distance_between_wheel)], 
                                 [self.radius_of_wheel*np.cos(pos_angle_)/2.0, self.radius_of_wheel*np.cos(pos_angle_)/2.0], 
                                 [self.radius_of_wheel*np.sin(pos_angle_)/2.0, self.radius_of_wheel*np.sin(pos_angle_)/2.0],
                                 [1.0, 0.0],
                                 [0.0, 1.0] ])
        pos = (state_matrix@vel_vector)
        pos = np.array([self.pos_angle+pos[0][0], self.pos_x+ pos[1][0], self.pos_y+ pos[2][0]])

        # print(pos)
        return pos
    
    def ForwardKinematics2(self, wheel_vel):
        """
        integrate as per Modern Robotics Forward Kinematics
        """
        r = self.radius_of_wheel
        d = self.distance_between_wheel
        u_x = wheel_vel[0][0]
        u_y = wheel_vel[1][0]
        # thetadot xdot ydot
        V = np.array([-r*u_x/(2.0*d)+r*u_y/(2.0*d), r*u_x/2.0 + r*u_y/2.0, 0])

        delq_b = [0, 0, 0]
        if V[0]==0.0:
            delq_b[0] = 0
            delq_b[1] = V[1]
            delq_b[2] = V[2]
            
        else:
            delq_b[0] = V[0]
            delq_b[1] = (V[1]*np.sin(V[0])+V[2]*(np.cos(V[0])-1))/V[0]
            delq_b[2] = (V[2]*np.sin(V[0])+V[1]*(1 - np.cos(V[0])))/V[0]

        phi = self.pos_angle
        x = self.pos_x
        y = self.pos_y
        mult_mat = np.array([[1, 0, 0], [0, np.cos(phi), -np.sin(phi)], [0, np.sin(phi), np.cos(phi)]])
        delq = mult_mat@np.array([[delq_b[0]], [delq_b[1]], [delq_b[2]]])
        pos = np.array([phi+delq[0][0], x+delq[1][0], y+delq[2][0]])
        return pos
        


def convert_list_to_dict(lst):
    """
    Converts a list to dictionary    
    """
    res_dct = {str(i): lst[i] for i in range(0, len(lst))}
    return res_dct


def transform_from_map_to_base(pos_x:float, pos_y:float, angle:float):
    """
    Transform from map frame to base
    """
    vec_m = np.array([[pos_x],[pos_y], [1]])
    T_bm = np.array([[1, 0, -(config.arena_length / 2)],
                    [0, 1, -(config.arena_width / 2)],
                    [0, 0, 1]])

    vec_b = T_bm @ vec_m

    return vec_b[0][0], vec_b[1][0], angle


def msg_decode(msg: bytes) -> list:
    """
    Decodes the message
    """
    packet = msg.decode('utf-8')
    # print("Stuff")
    # print(packet)
    result = [_.start() for _ in re.finditer('0b', packet)] 
    
    result.append(len(packet))
    data_arr = []
    for i in range(len(result)-1):
        num = packet[result[i]:result[i+1]]
        if num[2] == '1' or num[2] == '0':
            # print(num)
            num = int(num,2)
            data_arr.append(num) 
        else:
            data_arr.append(num[2:])
    # print(data_arr)
    return data_arr

class BotSim:
    """
    Defines Bot Class for simulation visualization   
    """
    def __init__(self, id):
        self.id = id
        self.usr_led = (0,0,0)
        self.pos_x = 0
        self.pos_y = 0
        self.angle = 0


def conv_to_json(robot_state, num_of_robot:int)->dict:
    """
    Convert data to dict so that it can be json dumped
    """
    json_dict = {}
    for i in range(0,num_of_robot):
        robot = BotSim(id=robot_state[i].id)
        robot.pos_x = robot_state[i].pos_x
        robot.pos_y = robot_state[i].pos_y
        robot.angle = robot_state[i].pos_angle
        robot.angle = (robot.angle + np.pi) % (2 * np.pi) - np.pi
        robot.usr_led = robot_state[i].usr_led
        json_dict[i] = robot.__dict__
    # print(json_dict)
    return json_dict

def update_time(robot_state:list, num_of_robot:int, sim_time:float)-> list:
    """
    Updates time of robot clocks
    """
    for i in range(0,num_of_robot):
        if robot_state[i].clk < sim_time:
           robot_state[i].clk = sim_time
    
    return robot_state

def update_msg_buffer(msg_buffer:list, MSG_BUFFER_SIZE:int, num_of_robot:int,msg:bytes, robot_id:int,robot_states:list)->list:
    """
    Update Message Buffer
    """
    ref_x = robot_states[robot_id].pos_x
    ref_y = robot_states[robot_id].pos_y
    # print("In update_msg_buffer")

    if robot_id == num_of_robot:
        range_of_val = range(0,robot_id)
    else:
        range_of_val = chain(range(0,robot_id-1),range(robot_id,num_of_robot))
    # print("Range of val:", range_of_val)
    for i in range_of_val:
        curr_pos_x = robot_states[i].pos_x
        curr_pos_y = robot_states[i].pos_y
        d = np.sqrt((ref_x - curr_pos_x)**2 + (ref_y - curr_pos_y)**2)
        if d < config.comm_range:
            random_bool = np.random.uniform() < config.packet_pass_rate
            if random_bool:
                if len(msg) > MSG_BUFFER_SIZE:
                    msg = msg[:MSG_BUFFER_SIZE]
                msg_buffer[i].append(msg)
                if len(msg_buffer[i])> config.msg_buffer_size:
                    msg_buffer[i] = msg_buffer[i][-config.msg_buffer_size:]
                
    # print(type(msg_buffer[2]))
    return msg_buffer

def initialize_robots():
    """
    Initialize the number of robots
    """
    flag = True
    vis_fd = -1
    vis_socket = None
    fd_to_id_map = {}
    num_of_robot = 0
    real_time_factor = config.real_time_factor
    robot = BotDiffDrive(id_=0)
    robot_state = [robot]*(config.num_robots)
    robot_id = -1*np.ones((config.num_robots))
    id_to_socket_map = {}
    while flag:
        try:
            rlist, wlist, xlist = select.select([server_socket] + open_client_sockets, open_client_sockets, []) # apending reading n writing socket to list
            for current_socket in rlist: 
                if current_socket is server_socket: # if there is a new client
                    (new_socket, address) = server_socket.accept() 
                    try:
                        data = new_socket.recv(1024)
                    except Exception:
                        data = '0b0'
                        data = data.encode('utf-8')
                    # sim_ticks +=1
                    if len(data) != 0:
                        msg = data.decode('utf-8')
                        
                        # print(msg)
                        if int(msg,2) ==7:
                            
                            
                            # print(fd_to_id_map)
                            id_to_socket_map[num_of_robot] = new_socket
                            # msg1 = str(bin(num_of_robot))
                            fd_to_id_map[new_socket.fileno()] = num_of_robot
                            # new_socket.sendall(msg1.encode('utf-8'))
                            robot_state[num_of_robot] = BotDiffDrive(id_=num_of_robot)

                            bot_init_pos = config.get_inital_pos_for_robot()
                            robot_state[num_of_robot].pos_x = bot_init_pos[0]
                            robot_state[num_of_robot].pos_y = bot_init_pos[1]
                            robot_state[num_of_robot].pos_angle = bot_init_pos[2]

                            if config.time_is_synced:
                                robot_state[num_of_robot].clk = 0
                            else:
                                val_ = random.uniform(0,1)*0.001
                                robot_state[num_of_robot].clk = val_

                            robot_id[num_of_robot] = num_of_robot
                            num_of_robot += 1
                        elif int(msg,2) == 5:
                            vis_fd = new_socket.fileno()
                            vis_socket = new_socket
                            msg1 = str(real_time_factor)
                            # print(vis_socket)
                            new_socket.sendall(msg1.encode('utf-8'))
                            print("Got vis connected")
                        elif msg == 1:
                            msg1 = str(bin(-1))
                            new_socket.sendall(msg.encode('utf-8'))
                    open_client_sockets.append(new_socket) # clients list
                
                
            
        except Exception:
            pass

        if config.use_visualizer:
            if num_of_robot == config.num_robots and vis_fd>0:
                # print("DONE")
                flag = False
        else:
            if num_of_robot == config.num_robots:
                flag = False

    print("While loop done")   
    for key, curr_socket in id_to_socket_map.items():
        msg1 = str(bin(int(key)))
        # print(msg1)
        # fd_to_id_map[new_socket.fileno()] = num_of_robot
        curr_socket.sendall(msg1.encode('utf-8'))
    
    # print(id_to_socket_map)
    
    # Make sure no robot is over another
    for i in range(0, num_of_robot):
        x1 = robot_state[i].pos_x
        y1 = robot_state[i].pos_y
        range_of_val = chain(range(0,i),range(i+1,num_of_robot))
        for j in range_of_val:
            x1_ = robot_state[j].pos_x
            y1_ = robot_state[j].pos_y
            d = np.sqrt((x1_-x1)**2 + (y1_ - y1)**2)
            if d <= config.robot_diameter:
                x1 = x1_ + config.robot_diameter + 0.02
                y1 = y1_ + config.robot_diameter + 0.02
            
        robot_state[i].pos_x = x1
        robot_state[i].pos_y = y1
    
    return vis_fd, vis_socket, fd_to_id_map, robot_state, robot_id

def check_collision(pos, robot_states, i, num_of_robot):
    """
    Checks for the collision of the robot
    """
    range_of_val = chain(range(0,i),range(i+1,num_of_robot))
    collision_flag_ = True
    for j in range_of_val:
        x1_ = robot_states[j].pos_x
        y1_ = robot_states[j].pos_y
        d = np.sqrt((x1_-pos[1])**2 + (y1_ - pos[2])**2)
        if d <= config.robot_diameter:
            collision_flag_ = False
    
    return collision_flag_

def integrate_world(robot_states:list, num_of_robot:int, wheel_vel_arr:list, curr_time, prev_time, dt, sim_time):
    """ 
    Integrates the world
    """
    # delta_time = real_time_factor*(curr_time - prev_time)
    delta_time = dt
    for i in range(0, num_of_robot):
        wheel_vel = wheel_vel_arr[i]
        u_l = wheel_vel[0]
        u_r = wheel_vel[1]
        pos = robot_states[i].integrate(u_l,u_r,delta_time)
    
        # check for collision with robots
        robot_states[i].pos_angle = pos[0]

        if check_collision(pos, robot_states, i, num_of_robot):
            robot_states[i].pos_x = pos[1]
            robot_states[i].pos_y = pos[2]
        # robot_states[i].pos_x = pos[1]
        # robot_states[i].pos_y = pos[2]

        # check for collision with walls
        robot_states[i].pos_x = max(robot_states[i].pos_x, -config.arena_width/2 + config.robot_radius)
        robot_states[i].pos_x = min(robot_states[i].pos_x, config.arena_width/2 - config.robot_radius)
        
        robot_states[i].pos_y = max(robot_states[i].pos_y, -config.arena_height/2 + config.robot_radius)
        robot_states[i].pos_y = min(robot_states[i].pos_y, config.arena_height/2 - config.robot_radius)

        robot_states[i].clk = max(robot_states[i].clk, sim_time)

    return robot_states

def send_data_to_vis(vis_socket, robot_state, num_of_robot, sim_time_curr, real_time_curr, actual_rtf)->None:
    """
    Send data to visualization
    """
    msg1 = conv_to_json(robot_state, num_of_robot)
    vis_socket.sendall(json.dumps(msg1).encode('utf-8'))
    recv_msg = vis_socket.recv(1024)
    # Send time to visualization
    _data_arr = [sim_time_curr, real_time_curr, actual_rtf]
    _data_json = json.dumps(_data_arr)
    vis_socket.sendall(_data_json.encode('utf-8'))

def get_data(current_socket, msg, robot_state, robot_id, num_of_robot, MSG_BUFFER_SIZE, msg_buffer, wheel_vel_arr):
    """
    Get and manage the data from sockets
    """
    
    if msg[2] == 3:
        # delay
        robot_state[int(msg[1])].clk += (msg[3]/1000)
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
    
    elif msg[2] == 2:
        # set_led
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        if msg[1] in robot_id:
            usr_led_ = (msg[3],msg[4],msg[5])
            robot_state[int(msg[1])].usr_led = usr_led_

    elif msg[2] == 4:
        # send_msg
        # print('Message sent:',msg[3])
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        msg_for_buffer = current_socket.recv(1024*4)
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        msg_type = msg[3]
        msg_buffer = update_msg_buffer(msg_buffer,MSG_BUFFER_SIZE,num_of_robot,msg_for_buffer,msg[1],robot_state)
        # print("New msg buffer:", msg_buffer)
        
    elif msg[2] == 5:
        # recv_msg
        arr = msg_buffer[msg[1]]
        
        len_ =len(arr)
        size_ = len_
        data_string = str(size_)
        current_socket.sendall(data_string.encode('utf-8'))
        # print(type(msg_buffer))
        clear_bool = current_socket.recv(1024)
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        # start_j = 0
        # end_j = 20

        for j in range(len_):
            check_data_ = current_socket.recv(1024)
            # data_send = convert_list_to_dict(arr[j])
            data = arr[j]
            # print(type(data))
            # start_j+=20
            # end_j = min(end_j+20, len_)
            # data = json.dumps(data_send)
            # print(data)
            current_socket.sendall(data)

        if clear_bool.decode('utf-8') == 'True':
            msg_buffer[msg[1]] = []
    elif msg[2] == 6:
        # get_clock
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        type_time = current_socket.recv(1024)
        time_val = robot_state[int(msg[1])].clk
        time_val = str(round(time_val,4))
        current_socket.sendall(time_val.encode('utf-8'))
    elif msg[2] == 7:
        # Set wheel velocity
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        vel = current_socket.recv(1024)
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        vel = vel.decode('utf-8')
        vel = json.loads(vel)
        # print(vel)
        wheel_pow = np.array([vel[0],vel[1]])
        # print("Wheel power:", wheel_pow)
        # print("motor_full_speed", motor_full_speed)
        wheel_vel = config.max_motor_speed * wheel_pow / 100
        wheel_vel_arr[int(msg[1])] = wheel_vel
        # print("Wheel velocity:",wheel_vel)
    elif msg[2] == 8:
        # get_pose
        data_string = '0b1'
        current_socket.sendall(data_string.encode('utf-8'))
        pose_type = current_socket.recv(1024)
        local_id = int(msg[1])
        pos_tuple = [robot_state[local_id].pos_x, robot_state[local_id].pos_y, robot_state[local_id].pos_angle]
        # x_, y_, theta_ = transform_from_map_to_base(pos_tuple[0], pos"Some error"_tuple[1], pos_tuple[2])
        # pos_tuple = [x_,y_,theta_]
        pos_tuple = json.dumps(pos_tuple)
        current_socket.sendall(pos_tuple.encode('utf-8'))

    return robot_state, msg_buffer, wheel_vel_arr

def log_data(robot_state, num_of_robot, sim_time_curr, real_time_curr, actual_rtf)->None:
    """
    Logs data to sim.log
    """
    log_data = conv_to_json(robot_state, num_of_robot)
    log_data['sim_time'] = sim_time_curr
    log_data['real_time'] = real_time_curr
    log_data['real_time_factor'] = round(actual_rtf,1)
    log_data = json.dumps(log_data)
    log_obj.info(log_data)

def loop():
    """
    Loop through to get data from bot classes
    """
    sim_time_start = time.time()
    notslept = 0
    real_time_factor = config.real_time_factor
    
    T_sim = config.sim_time_step
    # T_sim = real_time_factor*T_real
    T_real = T_sim/real_time_factor
<<<<<<< HEAD
    
    sim_time_curr = 0.0001
    
=======
    sim_time_curr = 0.0001
>>>>>>> origin/user/markovejnovic/13-performance-fix
    buffer_list_size = 16
    delta_vis = 0
    real_time_curr = 0
    sim_time_delt = 0.0
    msg_buffer = [[]] * config.num_robots
    MSG_BUFFER_SIZE = 1792
    num_of_robot = config.num_robots
    wheel_vel_arr  = [np.array([0,0])]*(config.num_robots)
    vis_fd, vis_socket, fd_to_id_map, robot_state, robot_id = initialize_robots()
    real_time_now_start = time.time()

    actual_rtf = real_time_factor

    killer = GracefulKiller()
    actual_rtf_list = []
    while not killer.kill_now:
        _time_socket_start = time.time()
        rlist, wlist, xlist = select.select([server_socket] + open_client_sockets, open_client_sockets, []) # apending reading n writing socket to list
        # Loop through the sockets and get all the data
        for current_socket in rlist: # sockets that can be read
            # print("In Loop")
            if current_socket.fileno() == vis_fd:
                continue

            if current_socket.fileno() in fd_to_id_map.keys():
                
                if robot_state[fd_to_id_map[current_socket.fileno()]].clk > sim_time_curr:
                    continue
                # print(current_socket.fileno())
            data = current_socket.recv(4*1024)
            if len(data) == 0:
                print("Gibberish")
            else:
                msg = msg_decode(data)
                robot_state, msg_buffer, wheel_vel_arr = get_data(current_socket, msg, robot_state, robot_id, num_of_robot, MSG_BUFFER_SIZE, msg_buffer, wheel_vel_arr)
                
        _time_socket_delta = time.time() - _time_socket_start 
        
        # Only allows visualization every 0.05 seconds
        delta_vis += T_real
        if config.use_visualizer == 1:

            if delta_vis > 0.05: 
                delta_vis = 0
                if vis_fd>0:
                    rtf = np.mean(actual_rtf_list)
                    actual_rtf_list = []
                    send_data_to_vis(vis_socket, robot_state, num_of_robot, sim_time_curr, real_time_curr, rtf)
        else:
            # Log stuff here 
            log_data(robot_state, num_of_robot, sim_time_curr, real_time_curr, actual_rtf)
            
        
        _time_integrate_start = time.time()
        sim_time_curr += T_sim
        robot_state = integrate_world(robot_state, num_of_robot, wheel_vel_arr, curr_time = time.time(), prev_time = real_time_now_start, dt = T_sim, sim_time= sim_time_curr)
        
        real_time_now_end = time.time()
        elapsed_time_diff = real_time_now_end - real_time_now_start
        _time_integrate_delta = time.time() - _time_integrate_start
        real_time_now_start = time.time()

        if elapsed_time_diff < T_real:
            real_time_curr += T_real
            actual_rtf = T_sim/T_real
            diff = T_real - elapsed_time_diff
            if diff>0:
                time.sleep(diff)
        else:
            
            real_time_curr += elapsed_time_diff
            actual_rtf = T_sim/elapsed_time_diff
            elapsedDIffList.append(elapsed_time_diff)
            
            notslept += 1
<<<<<<< HEAD
            # print(notslept)
        actual_rtf_list.append(actual_rtf)
    server_socket.close()

def main():

    functiontrace.trace()
=======
    server_socket.close()

def main() -> int:
    # functiontrace.trace()

>>>>>>> origin/user/markovejnovic/13-performance-fix
    try:
       loop()
    except KeyboardInterrupt:
        print("Client Shutdown requested...exiting")
        server_socket.close()
    except BaseException:
        print("BaseException")

    return 0


if __name__ == "__main__":
    sys.exit(main())
