# 8 Track Automated Filament Changer
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math, logging
import stepper, chelper
import time
from . import output_pin
from kinematics import extruder
from . import AFC_assist


#LED
BACKGROUND_PRIORITY_CLOCK = 0x7fffffff00000000
BIT_MAX_TIME=.000004
RESET_MIN_TIME=.000050
MAX_MCU_SIZE = 500  # Sanity check on LED chain length

def calc_move_time(dist, speed, accel):
    """
    Calculate the movement time and parameters for a given distance, speed, and acceleration.

    This function computes the axis direction, acceleration time, cruise time, and cruise speed
    required to move a specified distance with given speed and acceleration.

    Parameters:
    dist (float): The distance to move.
    speed (float): The speed of the movement.
    accel (float): The acceleration of the movement.

    Returns:
    tuple: A tuple containing:
        - axis_r (float): The direction of the axis (1 for positive, -1 for negative).
        - accel_t (float): The time spent accelerating.
        - cruise_t (float): The time spent cruising at constant speed.
        - speed (float): The cruise speed.
    """
    axis_r = 1.
    if dist < 0.:
        axis_r = -1.
        dist = -dist
    if not accel or not dist:
        return axis_r, 0., dist / speed, speed
    max_cruise_v2 = dist * accel
    if max_cruise_v2 < speed**2:
        speed = math.sqrt(max_cruise_v2)
    accel_t = speed / accel
    accel_decel_d = accel_t * speed
    cruise_t = (dist - accel_decel_d) / speed
    return axis_r, accel_t, cruise_t, speed


class AFCExtruderStepper:
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.extruder_stepper = extruder.ExtruderStepper(config)
        self.extruder_name = config.get('extruder')
        self.name = config.get_name().split()[-1]
        self.motion_queue = None
        self.status = ''

        self.reactor = self.printer.get_reactor()
        ffi_main, ffi_lib = chelper.get_ffi()
        self.trapq = ffi_main.gc(ffi_lib.trapq_alloc(), ffi_lib.trapq_free)
        self.trapq_append = ffi_lib.trapq_append
        self.trapq_finalize_moves = ffi_lib.trapq_finalize_moves
        self.stepper_kinematics = ffi_main.gc(
            ffi_lib.cartesian_stepper_alloc(b'x'), ffi_lib.free)

        self.gcode = self.printer.lookup_object('gcode')
        
        self.hub_dist = config.getfloat('hub_dist')
        
        #
        self.dist_hub = config.getfloat('dist_hub', 60)
        # LEDS
        self.led_index = config.get('led_index')

        # lane triggers
        buttons = self.printer.load_object(config, "buttons")

        self.prep = config.get('prep', None)
        if self.prep is not None:
            self.prep_state = False
            buttons.register_buttons([self.prep], self.prep_callback)

        self.load = config.get('load', None)
        if self.load is not None:
            self.load_state = False
            buttons.register_buttons([self.load], self.load_callback)
            
        # Respoolers
        self.afc_motor_rwd = config.get('afc_motor_rwd', None)
        self.afc_motor_fwd = config.get('afc_motor_fwd', None)
        self.afc_motor_enb = config.get('afc_motor_enb', None)
        if self.afc_motor_rwd is not None:
            self.afc_motor_rwd = AFC_assist.AFCassistMotor(config,'rwd')
        if self.afc_motor_fwd is not None:
            self.afc_motor_fwd = AFC_assist.AFCassistMotor(config,'fwd')
        if self.afc_motor_enb is not None:
            self.afc_motor_enb = AFC_assist.AFCassistMotor(config,'enb')
            
        self.AFC = self.printer.lookup_object('AFC')
        self.gcode = self.printer.lookup_object('gcode')   

        # Defaulting to false so that extruder motors to not move until PREP has been called
        self._afc_prep_done = False

    def assist(self, value, is_resend=False):
        self.gcode.respond_info('Testing ' + self.name+ ' at '+ str(value))
        if self.afc_motor_rwd is None:
            return
        if value < 0:
            value *= -1
            assit_motor=self.afc_motor_rwd
        else:
            if self.afc_motor_fwd is None:
                assit_motor=self.afc_motor_rwd
            else:
                assit_motor=self.afc_motor_fwd
        value /= assit_motor.scale
        if not assit_motor.is_pwm and value not in [0., 1.]:
            if value > 0:
                value = 1
        # Obtain print_time and apply requested settings
        toolhead = self.printer.lookup_object('toolhead')
        if self.afc_motor_enb is not None:
            if value != 0:
                enable = 1
            else:
                enable = 0
            toolhead.register_lookahead_callback(
            lambda print_time: self.afc_motor_enb._set_pin(print_time, enable))
            
        toolhead.register_lookahead_callback(
            lambda print_time: assit_motor._set_pin(print_time, value))

    def move(self, distance, speed, accel):
        """
        Move the specified lane a given distance with specified speed and acceleration.

        This function calculates the movement parameters and commands the stepper motor
        to move the lane accordingly.

        Parameters:
        distance (float): The distance to move.
        speed (float): The speed of the movement.
        accel (float): The acceleration of the movement.
        """
        
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.flush_step_generation()
        prev_sk = self.extruder_stepper.stepper.set_stepper_kinematics(self.stepper_kinematics)
        prev_trapq = self.extruder_stepper.stepper.set_trapq(self.trapq)
        self.extruder_stepper.stepper.set_position((0., 0., 0.))
        axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, speed, accel)
        print_time = toolhead.get_last_move_time()
        self.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                          0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
        print_time = print_time + accel_t + cruise_t + accel_t
        self.extruder_stepper.stepper.generate_steps(print_time)
        self.trapq_finalize_moves(self.trapq, print_time + 99999.9,
                                  print_time + 99999.9)
        self.extruder_stepper.stepper.set_trapq(prev_trapq)
        self.extruder_stepper.stepper.set_stepper_kinematics(prev_sk)
        toolhead.note_mcu_movequeue_activity(print_time)
        toolhead.dwell(accel_t + cruise_t + accel_t)
        toolhead.flush_step_generation()

    def set_afc_prep_done(self):
        """
        set_afc_prep_done function should only be called once AFC PREP function is done. Once this
            function is called it sets afc_prep_done to True. Once this is done the prep_callback function will
            now load once filament is inserted. 
        """
        self._afc_prep_done = True
    def load_callback(self, eventtime, state):
        self.load_state = state

    def prep_callback(self, eventtime, state):
        self.prep_state = state

        # Checking to make sure printer is ready and making sure PREP has been called before trying to load anything
        if self.printer.state_message == 'Printer is ready' and True == self._afc_prep_done:
            led = self.led_index
            if self.prep_state == True:
                x = 0
                while self.load_state == False and self.prep_state == True and self.status == '' :
                    x += 1
                    self.gcode.run_script_from_command('SET_STEPPER_ENABLE STEPPER="AFC_stepper '+self.name +'" ENABLE=1')
                    self.AFC.afc_move(self.name,10,500,400)
                    self.gcode.run_script_from_command('SET_STEPPER_ENABLE STEPPER="AFC_stepper '+self.name +'" ENABLE=0')
                    if x> 10:
                        msg = (' FAILED TO LOAD, CHECK FILAMENT AT TRIGGER\n||==>--||----||------||\nTRG   LOAD   HUB    TOOL')
                        self.AFC.respond_error(msg, raise_error=False)
                        self.AFC.afc_led(self.AFC.led_fault, led)
                        break
                if self.load_state == True and self.prep_state == True:
                    self.AFC.afc_led(self.AFC.led_ready, led)
            else:
                self.status = ''
                self.AFC.afc_led(self.AFC.led_not_ready, led)

def load_config_prefix(config):
    return AFCExtruderStepper(config)
