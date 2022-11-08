import os
import time
from abc import abstractmethod, ABC
from typing import Dict, Tuple, List, Callable

from cereal import car
from common.kalman.simple_kalman import KF1D
from common.numpy_fast import interp
from common.realtime import DT_CTRL
from selfdrive.car import gen_empty_fingerprint
from common.conversions import Conversions as CV
from selfdrive.controls.lib.drive_helpers import V_CRUISE_MAX, apply_deadzone
from selfdrive.controls.lib.events import Events
from selfdrive.controls.lib.vehicle_model import VehicleModel
from common.params import Params

GearShifter = car.CarState.GearShifter
EventName = car.CarEvent.EventName
TorqueFromLateralAccelCallbackType = Callable[[float, car.CarParams.LateralTorqueTuning, float, float, bool], float]

# WARNING: this value was determined based on the model's training distribution,
#MAX_CTRL_SPEED = (V_CRUISE_MAX + 4) * CV.KPH_TO_MS  # 144 + 4 = 92 mph
MAX_CTRL_SPEED = 161 * CV.KPH_TO_MS  # 144 + 4 = 92 mph
ACCEL_MAX = 2.0
ACCEL_MIN = -4.0
FRICTION_THRESHOLD = 0.3

# generic car and radar interfaces


class CarInterfaceBase(ABC):
  def __init__(self, CP, CarController, CarState):
    self.CP = CP
    self.VM = VehicleModel(CP)

    self.frame = 0
    self.steering_unpressed = 0
    self.low_speed_alert = False
    self.silent_steer_warning = True

    if CarState is not None:
      self.CS = CarState(CP)
      self.cp = self.CS.get_can_parser(CP)
      self.cp_cam = self.CS.get_cam_can_parser(CP)
      self.cp_body = self.CS.get_body_can_parser(CP)
      self.cp_loopback = self.CS.get_loopback_can_parser(CP)

    self.CC = None
    if CarController is not None:
      self.CC = CarController(self.cp.dbc_name, CP, self.VM)

    self.steer_warning_fix_enabled = Params().get_bool("SteerWarningFix")
    self.user_specific_feature = int(Params().get("UserSpecificFeature", encoding="utf8"))

  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    return ACCEL_MIN, ACCEL_MAX

  @staticmethod
  @abstractmethod
  def get_params(candidate, fingerprint=gen_empty_fingerprint(), car_fw=None, disable_radar=False):
    pass

  @staticmethod
  def init(CP, logcan, sendcan):
    pass

  @staticmethod
  def get_steer_feedforward_default(desired_angle, v_ego):
    # Proportional to realigning tire momentum: lateral acceleration.
    # TODO: something with lateralPlan.curvatureRates
    return desired_angle * (v_ego**2)

  @classmethod
  def get_steer_feedforward_function(self):
    return self.get_steer_feedforward_default

  @staticmethod
  def torque_from_lateral_accel_linear(lateral_accel_value, torque_params, lateral_accel_error, lateral_accel_deadzone, friction_compensation):
    # The default is a linear relationship between torque and lateral acceleration (accounting for road roll and steering friction)
    friction_interp = interp(
      apply_deadzone(lateral_accel_error, lateral_accel_deadzone),
      [-FRICTION_THRESHOLD, FRICTION_THRESHOLD],
      [-torque_params.friction, torque_params.friction]
    )
    friction = friction_interp if friction_compensation else 0.0
    return (lateral_accel_value / torque_params.latAccelFactor) + friction

  def torque_from_lateral_accel(self) -> TorqueFromLateralAccelCallbackType:
    return self.torque_from_lateral_accel_linear

  # returns a set of default params to avoid repetition in car specific params
  @staticmethod
  def get_std_params(candidate, fingerprint):
    ret = car.CarParams.new_message()
    ret.carFingerprint = candidate

    # standard ALC params
    ret.steerControlType = car.CarParams.SteerControlType.torque
    ret.minSteerSpeed = 0.
    ret.wheelSpeedFactor = 1.0

    ret.pcmCruise = True     # openpilot's state is tied to the PCM's cruise state on most cars
    ret.minEnableSpeed = -1. # enable is done by stock ACC, so ignore this
    ret.steerRatioRear = 0.  # no rear steering, at least on the listed cars aboveA
    ret.openpilotLongitudinalControl = False
    ret.stopAccel = -2.0
    ret.stoppingDecelRate = 0.8 # brake_travel/s while trying to stop
    ret.vEgoStopping = 0.7
    ret.vEgoStarting = 0.7
    ret.stoppingControl = True
    ret.longitudinalTuning.deadzoneBP = [0.]
    ret.longitudinalTuning.deadzoneV = [0.]
    ret.longitudinalTuning.kf = 1.
    ret.longitudinalTuning.kpBP = [0.]
    ret.longitudinalTuning.kpV = [1.]
    ret.longitudinalTuning.kiBP = [0.]
    ret.longitudinalTuning.kiV = [1.]
    ret.longitudinalTuning.kdBP = [0.]
    ret.longitudinalTuning.kdV = [0.]
    ret.longitudinalTuning.kfBP = [0.]
    ret.longitudinalTuning.kfV = [1.]
    # TODO estimate car specific lag, use .15s for now
    ret.longitudinalActuatorDelayLowerBound = 0.15
    ret.longitudinalActuatorDelayUpperBound = 0.15
    ret.steerLimitTimer = 1.0
    return ret

  @abstractmethod
  def update(self, c: car.CarControl, can_strings: List[bytes]) -> car.CarState:
    pass

  @abstractmethod
  def apply(self, c: car.CarControl) -> Tuple[car.CarControl.Actuators, List[bytes]]:
    pass

  def create_common_events(self, cs_out, extra_gears=None, pcm_enable=True):
    events = Events()

    if self.user_specific_feature == 11:
      if cs_out.gearShifter != GearShifter.drive and (extra_gears is None or
        cs_out.gearShifter not in extra_gears) and cs_out.cruiseState.enabled:
        events.add(EventName.gearNotD)
      if cs_out.gearShifter == GearShifter.reverse:
        events.add(EventName.reverseGear)
    else:
      if cs_out.doorOpen:
        events.add(EventName.doorOpen)
      if cs_out.seatbeltUnlatched:
        events.add(EventName.seatbeltNotLatched)
      if cs_out.gearShifter != GearShifter.drive and (extra_gears is None or
        cs_out.gearShifter not in extra_gears) and cs_out.cruiseState.enabled:
        events.add(EventName.wrongGear)
      if cs_out.gearShifter == GearShifter.reverse:
        events.add(EventName.reverseGear)
      if not cs_out.cruiseState.available and cs_out.cruiseState.enabled:
        events.add(EventName.wrongCarMode)
    if cs_out.espDisabled:
      events.add(EventName.espDisabled)
    #if cs_out.gasPressed:
    #  events.add(EventName.gasPressed)
    if cs_out.stockFcw:
      events.add(EventName.stockFcw)
    if cs_out.stockAeb:
      events.add(EventName.stockAeb)
    if cs_out.vEgo > MAX_CTRL_SPEED:
      events.add(EventName.speedTooHigh)
    # if cs_out.cruiseState.nonAdaptive:
    #   events.add(EventName.wrongCruiseMode)
    #if cs_out.brakeHoldActive and self.CP.openpilotLongitudinalControl:
    #  events.add(EventName.brakeHold)


    # Handle permanent and temporary steering faults
    self.steering_unpressed = 0 if cs_out.steeringPressed else self.steering_unpressed + 1
    if cs_out.steerFaultTemporary and not self.steer_warning_fix_enabled:
      # if the user overrode recently, show a less harsh alert
      if (cs_out.vEgo < 0.1 or cs_out.standstill) and cs_out.steeringAngleDeg < 90:
        events.add(EventName.isgActive)
      elif self.silent_steer_warning or cs_out.standstill or self.steering_unpressed < int(1.5 / DT_CTRL) and cs_out.vEgo > 1:
        self.silent_steer_warning = True
        events.add(EventName.steerTempUnavailableSilent)
      elif cs_out.vEgo > 1:
        events.add(EventName.steerTempUnavailable)
    elif cs_out.vEgo > 1:
      self.silent_steer_warning = False
    if cs_out.steerFaultPermanent and cs_out.vEgo > 1:
      events.add(EventName.steerUnavailable)

    # Disable on rising edge of gas or brake. Also disable on brake when speed > 0.
    # Optionally allow to press gas at zero speed to resume.
    # e.g. Chrysler does not spam the resume button yet, so resuming with gas is handy. FIXME!
    # if (cs_out.gasPressed and (not self.CS.out.gasPressed) and cs_out.vEgo > gas_resume_speed) or \
    #    (cs_out.brakePressed and (not self.CS.out.brakePressed or not cs_out.standstill)):
    #   events.add(EventName.pedalPressed)

    # we engage when pcm is active (rising edge)
    if pcm_enable:
      if cs_out.cruiseState.enabled and not self.CS.out.cruiseState.enabled:
        events.add(EventName.pcmEnable)
      elif not cs_out.cruiseState.enabled:
        events.add(EventName.pcmDisable)

    return events


class RadarInterfaceBase(ABC):
  def __init__(self, CP):
    self.pts = {}
    self.delay = 0
    self.radar_ts = CP.radarTimeStep
    self.no_radar_sleep = 'NO_RADAR_SLEEP' in os.environ

  def update(self, can_strings):
    ret = car.RadarData.new_message()
    if not self.no_radar_sleep:
      time.sleep(self.radar_ts)  # radard runs on RI updates
    return ret


class CarStateBase(ABC):
  def __init__(self, CP):
    self.CP = CP
    self.car_fingerprint = CP.carFingerprint
    self.out = car.CarState.new_message()

    self.cruise_buttons = 0
    self.left_blinker_cnt = 0
    self.right_blinker_cnt = 0
    self.left_blinker_prev = False
    self.right_blinker_prev = False

    # Q = np.matrix([[0.0, 0.0], [0.0, 100.0]])
    # R = 0.3
    self.v_ego_kf = KF1D(x0=[[0.0], [0.0]],
                         A=[[1.0, DT_CTRL], [0.0, 1.0]],
                         C=[1.0, 0.0],
                         K=[[0.17406039], [1.65925647]])

  def update_speed_kf(self, v_ego_raw):
    if abs(v_ego_raw - self.v_ego_kf.x[0][0]) > 2.0:  # Prevent large accelerations when car starts at non zero speed
      self.v_ego_kf.x = [[v_ego_raw], [0.0]]

    v_ego_x = self.v_ego_kf.update(v_ego_raw)
    return float(v_ego_x[0]), float(v_ego_x[1])

  def get_wheel_speeds(self, fl, fr, rl, rr, unit=CV.KPH_TO_MS):
    factor = unit * self.CP.wheelSpeedFactor

    wheelSpeeds = car.CarState.WheelSpeeds.new_message()
    wheelSpeeds.fl = fl * factor
    wheelSpeeds.fr = fr * factor
    wheelSpeeds.rl = rl * factor
    wheelSpeeds.rr = rr * factor
    return wheelSpeeds

  def update_blinker_from_lamp(self, blinker_time: int, left_blinker_lamp: bool, right_blinker_lamp: bool):
    """Update blinkers from lights. Enable output when light was seen within the last `blinker_time`
    iterations"""
    # TODO: Handle case when switching direction. Now both blinkers can be on at the same time
    self.left_blinker_cnt = blinker_time if left_blinker_lamp else max(self.left_blinker_cnt - 1, 0)
    self.right_blinker_cnt = blinker_time if right_blinker_lamp else max(self.right_blinker_cnt - 1, 0)
    return self.left_blinker_cnt > 0, self.right_blinker_cnt > 0

  def update_blinker_from_stalk(self, blinker_time: int, left_blinker_stalk: bool, right_blinker_stalk: bool):
    """Update blinkers from stalk position. When stalk is seen the blinker will be on for at least blinker_time,
    or until the stalk is turned off, whichever is longer. If the opposite stalk direction is seen the blinker
    is forced to the other side. On a rising edge of the stalk the timeout is reset."""

    if left_blinker_stalk:
      self.right_blinker_cnt = 0
      if not self.left_blinker_prev:
        self.left_blinker_cnt = blinker_time

    if right_blinker_stalk:
      self.left_blinker_cnt = 0
      if not self.right_blinker_prev:
        self.right_blinker_cnt = blinker_time

    self.left_blinker_cnt = max(self.left_blinker_cnt - 1, 0)
    self.right_blinker_cnt = max(self.right_blinker_cnt - 1, 0)

    self.left_blinker_prev = left_blinker_stalk
    self.right_blinker_prev = right_blinker_stalk

    return bool(left_blinker_stalk or self.left_blinker_cnt > 0), bool(right_blinker_stalk or self.right_blinker_cnt > 0)

  @staticmethod
  def parse_gear_shifter(gear: str) -> car.CarState.GearShifter:
    d: Dict[str, car.CarState.GearShifter] = {
        'P': GearShifter.park, 'R': GearShifter.reverse, 'N': GearShifter.neutral,
        'E': GearShifter.eco, 'T': GearShifter.manumatic, 'D': GearShifter.drive,
        'S': GearShifter.sport, 'L': GearShifter.low, 'B': GearShifter.brake
    }
    return d.get(gear, GearShifter.unknown)

  @staticmethod
  def get_cam_can_parser(CP):
    return None

  @staticmethod
  def get_body_can_parser(CP):
    return None

  @staticmethod
  def get_loopback_can_parser(CP):
    return None
