"""
Traffic Awareness System - TrafficStateManager
Monitors FSLTL AI traffic via SimConnect and detects state changes.
"""
import threading
import time
import math
import random
import hashlib
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from .context import event_bus, shared_context, context_lock

class TrafficState(Enum):
    """State machine states for AI aircraft."""
    UNKNOWN = auto()
    PARKED = auto()
    PUSHBACK = auto()
    TAXIING = auto()
    TAKEOFF_ROLL = auto()
    AIRBORNE = auto()
    APPROACH = auto()
    LANDING = auto()
    VACATING = auto()

@dataclass
class AircraftTrackingData:
    """Tracking data for a single AI aircraft."""
    callsign: str
    state: TrafficState = TrafficState.UNKNOWN
    pending_state: Optional[TrafficState] = None  # State waiting for hysteresis confirmation
    pending_state_start: float = 0.0  # When pending state was first detected

    # Telemetry
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    heading: float = 0.0
    airspeed: float = 0.0
    vertical_speed: float = 0.0
    on_ground: bool = True

    # Previous frame data (for change detection)
    prev_latitude: float = 0.0
    prev_longitude: float = 0.0
    prev_heading: float = 0.0
    prev_on_ground: bool = True

    # Metadata
    last_seen: float = field(default_factory=time.time)
    voice_id: Optional[str] = None  # Assigned TTS voice

    # C1/C2: SimConnect AI 枚举补充字段（X-Plane/mock 留空）
    aircraft_type: str = ""            # ATC TYPE（ICAO 机型，如 B738）
    wake_category: str = "UNKNOWN"     # 由 ATC TYPE 推导（ICAO Doc 4444）
    assigned_runway: Optional[str] = None   # AI_TRAFFIC_ASSIGNED_RUNWAY
    assigned_parking: Optional[str] = None  # AI_TRAFFIC_ASSIGNED_PARKING
    icao_dest: Optional[str] = None         # AI_TRAFFIC_TOAIRPORT
    eta_s: Optional[float] = None           # AI_TRAFFIC_ETA（秒）

class TrafficStateManager:
    """
    Manages tracking and state detection for AI traffic.
    Scans SimConnect for AI objects and emits events on state changes.
    """
    
    # Hysteresis threshold - state must persist for this long before confirmation
    HYSTERESIS_SECONDS = 2.0
    
    # Teleport detection threshold (nautical miles)
    TELEPORT_THRESHOLD_NM = 5.0
    
    # Stale aircraft cleanup time (seconds without update)
    STALE_TIMEOUT = 30.0
    
    # Scan interval
    SCAN_INTERVAL = 0.5  # 2 Hz
    
    def __init__(self, config, sim_bridge, socketio=None):
        self.config = config
        self.sim_bridge = sim_bridge  # Need access to SimConnect instance
        self.socketio = socketio  # For direct Socket.IO emission
        self.aircraft: Dict[str, AircraftTrackingData] = {}
        self.lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.enabled = config.get('traffic', {}).get('enabled', True)
        self.self_managed_enabled = config.get('traffic', {}).get('self_managed_enabled', True)
        self.export_limit = int(config.get('traffic', {}).get('export_limit', 63) or 63)
        # C1/C2：MSFS AI 交通枚举读取器（独立 SimConnect 连接，线程隔离）
        self.msfs_ai_enabled = bool(config.get('traffic', {}).get('msfs_ai_enabled', True))
        self._traffic_reader = None
        self._reader_failed = False

        print("TrafficStateManager: Initialized.")

    @property
    def traffic_source_state(self) -> str:
        """当前交通源形态：live / static / none / unavailable（E11）。"""
        reader = self._traffic_reader
        if reader is None or not getattr(reader, 'available', False):
            return 'unavailable'
        return getattr(reader, 'source_state', 'unavailable')

    def _ensure_traffic_reader(self):
        """惰性创建 AI 交通读取器。失败只记录一次并保持 None（降级 mock）。"""
        if self._traffic_reader is not None or self._reader_failed:
            return self._traffic_reader
        if not self.msfs_ai_enabled:
            self._reader_failed = True
            return None
        try:
            from .simconnect_traffic import SimConnectTrafficReader
        except Exception as e:
            print(f"TrafficStateManager: simconnect_traffic import failed — {e}")
            self._reader_failed = True
            return None
        reader = SimConnectTrafficReader(self.config)
        if not reader.start():
            self._reader_failed = True
            return None
        self._traffic_reader = reader
        return reader
    
    def start(self):
        if self.enabled:
            self._thread.start()
            print("TrafficStateManager: Scanning thread started.")
        else:
            print("TrafficStateManager: Disabled in config.")
    
    def stop(self):
        self._stop_event.set()
        reader = self._traffic_reader
        if reader is not None:
            try:
                reader.stop()
            except Exception as e:
                print(f"TrafficStateManager: reader stop error: {e}")
    
    def _loop(self):
        """Main scanning loop."""
        last_bulk_update = 0
        last_source_emit = 0
        self._xplane_tcas_failed = False  # flip to True only after confirmed failure

        while not self._stop_event.is_set():
            now = time.time()

            # 1. Scan / Update Traffic
            if self.config.get('debug', {}).get('mock_mode', False):
                self._generate_mock_traffic()
                self._cleanup_stale()
            elif self._is_xplane():
                # X-Plane: try real TCAS first, fall back to mock only if unavailable
                if self.sim_bridge and self.sim_bridge.connected and not self._xplane_tcas_failed:
                    try:
                        self._scan_xplane_tcas()
                        self._cleanup_stale()
                    except Exception as e:
                        print(f"TrafficStateManager: X-Plane TCAS scan failed ({e}), falling back to mock")
                        self._xplane_tcas_failed = True
                        self._generate_enhanced_mock_traffic()
                        self._cleanup_stale()
                else:
                    self._generate_enhanced_mock_traffic()
                    self._cleanup_stale()
            elif self.sim_bridge and self.sim_bridge.connected:
                try:
                    self._scan_traffic()
                    self._cleanup_stale()
                except Exception as e:
                    print(f"TrafficStateManager Error: {e}")

            # 2. Bulk Event Emission (1Hz)
            if now - last_bulk_update > 1.0:
                self._emit_bulk_update()
                last_bulk_update = now

            # 3. 交通源形态（C3/E11）：每 5 秒推一次给 UI 状态栏
            if now - last_source_emit > 5.0:
                last_source_emit = now
                if self.socketio:
                    try:
                        self.socketio.emit('traffic_source_state', {
                            'state': self.traffic_source_state,
                            'ai_count': len(self.aircraft),
                            'msfs_ai_enabled': self.msfs_ai_enabled,
                        })
                    except Exception:
                        pass

            time.sleep(self.SCAN_INTERVAL)

    def _is_xplane(self) -> bool:
        """Return True when the active simulator is X-Plane."""
        simulator_provider = ((self.config.get('simulator', {}) or {}).get('provider') or 'auto').lower()
        if simulator_provider == 'xplane':
            return True
        provider_obj = getattr(self.sim_bridge, 'provider', None)
        provider_name = getattr(provider_obj, 'name', '').lower() if provider_obj else ''
        return 'x-plane' in provider_name

    def _should_use_self_managed_traffic(self) -> bool:
        """Kept for compatibility; internal logic now routes through _is_xplane()."""
        return False
            
    def _generate_mock_traffic(self):
        """Generate simulated traffic for testing Radar/Chatter."""
        # Initialize mock state if needed
        if not hasattr(self, '_mock_aircraft'):
            self._mock_aircraft = {} # {callsign: {type, start_time, params}}
            self._last_mock_spawn = 0

        now = time.time()
        
        # 1. Spawn new aircraft (max 5, every 10s)
        if len(self._mock_aircraft) < 5 and now - self._last_mock_spawn > 10:
            self._spawn_mock_aircraft(now)
            self._last_mock_spawn = now
            
        # 2. Update existing aircraft
        with context_lock:
            own_lat = shared_context['aircraft'].get('latitude', 40.08)
            own_lon = shared_context['aircraft'].get('longitude', 116.58)
            own_alt = shared_context['aircraft'].get('altitude', 10000)

        dead_aircraft = []
        
        for callsign, data in self._mock_aircraft.items():
            age = now - data['start_time']
            p = data['params']
            
            # Scenario A: Orbit (Type 0)
            if data['type'] == 0:
                angle = (age * 0.1 + p['phase']) % (2 * math.pi)
                radius = 0.05 # ~3nm
                lat = own_lat + math.sin(angle) * radius
                lon = own_lon + math.cos(angle) * radius
                hdg = (math.degrees(angle) + 90) % 360
                alt = own_alt + p['alt_offset']
                
                self.update_aircraft(callsign, {
                    'latitude': lat, 'longitude': lon, 'altitude': alt,
                    'heading': hdg, 'airspeed': 220, 'vertical_speed': 0, 'on_ground': False
                })
                
                # Despawn after 120s
                if age > 120: dead_aircraft.append(callsign)

            # Scenario B: Flyover (Type 1)
            elif data['type'] == 1:
                # Linear movement needed... simplifying to just linear lat/lon change
                # dx/dy per second
                lat = own_lat + p['start_lat_offset'] + (p['d_lat'] * age)
                lon = own_lon + p['start_lon_offset'] + (p['d_lon'] * age)
                
                # Check bounds (approx 10nm = 0.16 deg)
                if abs(lat - own_lat) > 0.2 or abs(lon - own_lon) > 0.2:
                    dead_aircraft.append(callsign)
                else:
                    self.update_aircraft(callsign, {
                        'latitude': lat, 'longitude': lon, 'altitude': p['alt'],
                        'heading': p['hdg'], 'airspeed': 300, 'vertical_speed': 0, 'on_ground': False
                    })
                    
        # 3. Cleanup
        for cs in dead_aircraft:
            del self._mock_aircraft[cs]
            # Also remove from main tracker to clear radar
            with self.lock:
                if cs in self.aircraft:
                    del self.aircraft[cs]

    def _spawn_mock_aircraft(self, now):
        """Create a new random mock aircraft."""
        airlines = ['CCA', 'CES', 'CSN', 'CHH', 'CXA', 'CQN']
        callsign = f"{random.choice(airlines)}{random.randint(100, 9999)}"
        
        atype = random.choice([0, 1]) # 0=Orbit, 1=Flyover
        
        params = {}
        if atype == 0: # Orbit
            params = {
                'phase': random.uniform(0, 6.28),
                'alt_offset': random.choice([-1000, 1000, 2000]),
            }
        else: # Flyover
            angle = random.uniform(0, 6.28)
            dist = 0.15 # Spawn at edge
            params = {
                'start_lat_offset': math.sin(angle) * dist,
                'start_lon_offset': math.cos(angle) * dist,
                'd_lat': -math.sin(angle) * 0.001, # Fly towards center roughly
                'd_lon': -math.cos(angle) * 0.001,
                'alt': random.randint(100, 300) * 100,
                'hdg': (math.degrees(angle) + 180) % 360
            }
            
        self._mock_aircraft[callsign] = {
            'type': atype,
            'start_time': now,
            'params': params
        }
        print(f"TrafficStateManager: Spawning Mock Traffic {callsign} (Type {atype})")

    def _scan_traffic(self):
        """MSFS：用 SimConnectTrafficReader 枚举 AI 飞机（C1/C2）。

        旧实现 hasattr(sm,'get_ai_aircraft_list') 恒为假 -> 永远落 mock。
        现在：枚举成功且非空 -> update_aircraft；枚举失败 / 零交通且配置允许
        mock 时 -> 退回 enhanced mock。交通源形态记入日志（live/static/none）。
        """
        try:
            if not self.sim_bridge or not self.sim_bridge.connected:
                return

            reader = self._ensure_traffic_reader()
            if reader is None or not reader.available:
                # 读取器不可用（无 SimConnect / DLL 缺能力）：保持既有降级行为
                if not self.aircraft:
                    self._generate_enhanced_mock_traffic()
                return

            targets = reader.poll_once()
            if targets:
                for ai in targets:
                    if not ai.get('callsign'):
                        continue
                    self._process_ai_object(ai)
            elif not self.aircraft:
                # 无注入器 / 零交通现场：队列按"空队列即放行"处理（E11）
                if self.config.get('debug', {}).get('mock_traffic_fallback', True):
                    self._generate_enhanced_mock_traffic()
        except Exception as e:
            print(f"TrafficStateManager: Scan error: {e}")
            if not self.aircraft:
                self._generate_enhanced_mock_traffic()
    
    def _scan_xplane_tcas(self):
        """Read LiveTraffic / AI traffic from X-Plane TCAS target arrays via Web API."""
        provider = getattr(self.sim_bridge, 'provider', None)
        if provider is None or not provider.is_connected():
            raise ConnectionError("X-Plane provider not connected")

        if not hasattr(provider, 'get_traffic_targets'):
            raise NotImplementedError("Provider does not support get_traffic_targets()")

        targets = provider.get_traffic_targets()

        if not targets:
            # No targets yet — not an error; TCAS array may just be empty
            return

        seen_callsigns = set()
        for t in targets:
            callsign = t.get('callsign', '').strip()
            if not callsign:
                continue
            seen_callsigns.add(callsign)
            self.update_aircraft(callsign, {
                'latitude':      t.get('latitude', 0),
                'longitude':     t.get('longitude', 0),
                'altitude':      t.get('altitude', 0),
                'heading':       t.get('heading', 0),
                'airspeed':      t.get('airspeed', 0),
                'vertical_speed': t.get('vertical_speed', 0),
                'on_ground':     t.get('on_ground', False),
            })

        # Mark aircraft no longer in TCAS as stale (last_seen update skipped)
        # _cleanup_stale() will remove them after STALE_TIMEOUT seconds

    def _process_ai_object(self, ai_data):
        """处理单个 AI 对象（C1 的结构化 dict：callsign/位置/机型/尾流/分配跑道等）。"""
        callsign = ai_data.get('callsign', ai_data.get('atc_id', 'UNKNOWN'))
        if not callsign or callsign == 'UNKNOWN':
            return

        # Update traffic data
        self.update_aircraft(callsign, {
            'latitude': ai_data.get('latitude', 0),
            'longitude': ai_data.get('longitude', 0),
            'altitude': ai_data.get('altitude', 0),
            'heading': ai_data.get('heading', 0),
            'airspeed': ai_data.get('airspeed', 0),
            'vertical_speed': ai_data.get('vertical_speed', 0),
            'on_ground': ai_data.get('on_ground', True),
            'aircraft_type': ai_data.get('aircraft_type', ''),
            'wake_category': ai_data.get('wake_category', 'UNKNOWN'),
            'assigned_runway': ai_data.get('assigned_runway'),
            'assigned_parking': ai_data.get('assigned_parking'),
            'icao_dest': ai_data.get('icao_dest'),
            'eta_s': ai_data.get('eta_s'),
        })
    
    def _generate_enhanced_mock_traffic(self):
        """Generate more realistic mock traffic based on ownship position."""
        # This enhanced version creates traffic around airports and on airways
        if not hasattr(self, '_enhanced_mock_initialized'):
            self._enhanced_mock_initialized = True
            self._mock_aircraft = {}
            self._last_mock_spawn = 0
            print("TrafficStateManager: Using enhanced mock traffic mode")
        
        now = time.time()
        
        # Get ownship position
        with context_lock:
            own_lat = shared_context['aircraft'].get('latitude', 40.08)
            own_lon = shared_context['aircraft'].get('longitude', 116.58)
            own_alt = shared_context['aircraft'].get('altitude', 10000)
        
        # Spawn new aircraft (max 8, every 15s)
        if len(self._mock_aircraft) < 8 and now - self._last_mock_spawn > 15:
            self._spawn_enhanced_mock(own_lat, own_lon, own_alt, now)
            self._last_mock_spawn = now
        
        # Update existing mock aircraft
        self._update_enhanced_mock(own_lat, own_lon, now)
        
    def _emit_bulk_update(self):
        """Emit all traffic data for Radar UI."""
        with self.lock:
            traffic_list = []
            for callsign, ac in self.aircraft.items():
                traffic_list.append({
                    'callsign': callsign,
                    'lat': ac.latitude,
                    'lon': ac.longitude,
                    'alt': ac.altitude,
                    'hdg': ac.heading,
                    'spd': ac.airspeed,
                    'vs': ac.vertical_speed,
                    'state': ac.state.name,
                    'on_ground': ac.on_ground,
                    # C1/C2 新字段（X-Plane/mock 下为默认值）
                    'type': ac.aircraft_type,
                    'wake': ac.wake_category,
                    'rwy': ac.assigned_runway,
                    'stand': ac.assigned_parking,
                    'dest': ac.icao_dest,
                })

            if traffic_list:
                event_bus.emit('traffic_update', traffic_list)
                # Also emit directly to Socket.IO for frontend
                if self.socketio:
                    self.socketio.emit('traffic_update', traffic_list)

    def get_export_targets(self, limit=None):
        limit = int(limit or self.export_limit or 63)
        targets = []
        with self.lock:
            for idx, (callsign, ac) in enumerate(sorted(self.aircraft.items()), start=1):
                if idx > limit:
                    break
                targets.append({
                    'slot': idx,
                    'mode_s_id': self._mode_s_id_for_callsign(callsign),
                    'flight_id': callsign[:7],
                    'latitude': ac.latitude,
                    'longitude': ac.longitude,
                    'altitude_ft': ac.altitude,
                    'heading_deg': ac.heading,
                    'groundspeed_kt': ac.airspeed,
                    'vertical_speed_fpm': ac.vertical_speed,
                    'on_ground': ac.on_ground,
                    'state': ac.state.name,
                })
        return targets
    
    def update_aircraft(self, callsign: str, data: Dict[str, Any]):
        """
        Update tracking data for an aircraft and detect state changes.
        Called when new telemetry is received (from scan or external source).
        """
        with self.lock:
            if callsign not in self.aircraft:
                # New aircraft detected
                self.aircraft[callsign] = AircraftTrackingData(
                    callsign=callsign,
                    voice_id=self._assign_voice(callsign)
                )
                print(f"TrafficStateManager: New aircraft detected: {callsign}")
            
            ac = self.aircraft[callsign]
            
            # Store previous frame data
            ac.prev_latitude = ac.latitude
            ac.prev_longitude = ac.longitude
            ac.prev_heading = ac.heading
            ac.prev_on_ground = ac.on_ground
            
            # Update current data
            ac.latitude = data.get('latitude', ac.latitude)
            ac.longitude = data.get('longitude', ac.longitude)
            ac.altitude = data.get('altitude', ac.altitude)
            ac.heading = data.get('heading', ac.heading)
            ac.airspeed = data.get('airspeed', ac.airspeed)
            ac.vertical_speed = data.get('vertical_speed', ac.vertical_speed)
            ac.on_ground = data.get('on_ground', ac.on_ground)
            ac.last_seen = time.time()
            # C1/C2：机型/尾流/分配信息（仅 MSFS AI 枚举提供；X-Plane/mock 不含这些键）
            for attr, key in (('aircraft_type', 'aircraft_type'),
                              ('wake_category', 'wake_category'),
                              ('assigned_runway', 'assigned_runway'),
                              ('assigned_parking', 'assigned_parking'),
                              ('icao_dest', 'icao_dest'),
                              ('eta_s', 'eta_s')):
                if key in data and data[key] not in (None, ''):
                    setattr(ac, attr, data[key])
            
            # Check for teleport
            if self._check_teleport(ac):
                print(f"TrafficStateManager: {callsign} teleported - resetting state")
                ac.state = TrafficState.UNKNOWN
                ac.pending_state = None
                return
            
            # Infer new state
            new_state = self._infer_state(ac)
            
            # Apply hysteresis
            self._apply_hysteresis(ac, new_state)
    
    def _check_teleport(self, ac: AircraftTrackingData) -> bool:
        """Check if aircraft teleported (moved > 5nm in one frame)."""
        if ac.prev_latitude == 0 and ac.prev_longitude == 0:
            return False  # First update, no previous position
        
        distance = self._haversine_nm(
            ac.prev_latitude, ac.prev_longitude,
            ac.latitude, ac.longitude
        )
        return distance > self.TELEPORT_THRESHOLD_NM
    
    def _infer_state(self, ac: AircraftTrackingData) -> TrafficState:
        """Infer aircraft state from telemetry."""
        spd = ac.airspeed
        alt = ac.altitude
        vs = ac.vertical_speed
        on_ground = ac.on_ground
        
        # Ground states
        if on_ground:
            if spd < 1:
                return TrafficState.PARKED
            elif spd < 5:
                # Check if moving backwards (pushback)
                # Simplified: assume low speed forward = taxi, could add heading change detection
                return TrafficState.PUSHBACK if self._is_reversing(ac) else TrafficState.TAXIING
            elif spd < 40:
                return TrafficState.TAXIING
            else:
                return TrafficState.TAKEOFF_ROLL
        
        # Air states
        else:
            # Just took off
            if ac.prev_on_ground:
                return TrafficState.AIRBORNE
            
            # Approach detection: low altitude, descending
            if alt < 3000 and vs < -200:
                return TrafficState.APPROACH
            
            # General airborne
            return TrafficState.AIRBORNE
    
    def _is_reversing(self, ac: AircraftTrackingData) -> bool:
        """Check if aircraft is moving backwards (pushback)."""
        # Simplified heuristic: check if heading is opposite to movement direction
        # Full implementation would check ground track vs heading
        return False  # Placeholder
    
    def _apply_hysteresis(self, ac: AircraftTrackingData, new_state: TrafficState):
        """Apply hysteresis to prevent state flicker."""
        now = time.time()
        
        if new_state == ac.state:
            # State unchanged, clear pending
            ac.pending_state = None
            return
        
        if new_state == ac.pending_state:
            # Same pending state, check if hysteresis period elapsed
            if now - ac.pending_state_start >= self.HYSTERESIS_SECONDS:
                # Confirm state change
                old_state = ac.state
                ac.state = new_state
                ac.pending_state = None
                
                # Emit event
                self._emit_state_change(ac, old_state, new_state)
        else:
            # New pending state
            ac.pending_state = new_state
            ac.pending_state_start = now
    
    def _emit_state_change(self, ac: AircraftTrackingData, old_state: TrafficState, new_state: TrafficState):
        """Emit traffic event on confirmed state change."""
        event_data = {
            'callsign': ac.callsign,
            'old_state': old_state.name,
            'new_state': new_state.name,
            'latitude': ac.latitude,
            'longitude': ac.longitude,
            'altitude': ac.altitude,
            'heading': ac.heading,
            'airspeed': ac.airspeed,
            'voice_id': ac.voice_id
        }
        
        print(f"TrafficStateManager: {ac.callsign} state: {old_state.name} -> {new_state.name}")
        event_bus.emit('traffic_state_change', event_data)
    
    def _cleanup_stale(self):
        """Remove aircraft not seen for a while."""
        now = time.time()
        stale = []
        
        with self.lock:
            for callsign, ac in self.aircraft.items():
                if now - ac.last_seen > self.STALE_TIMEOUT:
                    stale.append(callsign)
            
            for callsign in stale:
                del self.aircraft[callsign]
                print(f"TrafficStateManager: Removed stale aircraft: {callsign}")
    
    def _assign_voice(self, callsign: str) -> str:
        """Assign a consistent TTS voice based on callsign hash."""
        voices = [
            "en-GB-RyanNeural",
            "en-US-GuyNeural",
            "en-AU-WilliamNeural",
            "en-IN-PrabhatNeural",
            "en-GB-ThomasNeural",
            "en-US-ChristopherNeural",
            "en-AU-NatashaNeural",
            "en-GB-SoniaNeural",
        ]
        hash_val = int(hashlib.md5(callsign.encode()).hexdigest(), 16)
        return voices[hash_val % len(voices)]

    def _mode_s_id_for_callsign(self, callsign: str) -> int:
        hash_val = int(hashlib.md5(callsign.encode()).hexdigest(), 16)
        return 1 + (hash_val % 16777214)
    
    def _haversine_nm(self, lat1, lon1, lat2, lon2) -> float:
        """Calculate distance between two points in nautical miles."""
        R = 3440.065  # Earth radius in nm
        
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        return R * c
    
    def get_traffic_in_context(self, frequency_type: str) -> list:
        """
        Get list of aircraft relevant to the current frequency context.
        
        frequency_type: 'ground', 'tower', 'approach', 'center'
        """
        result = []
        
        with self.lock:
            for callsign, ac in self.aircraft.items():
                if self._matches_frequency_context(ac, frequency_type):
                    result.append({
                        'callsign': callsign,
                        'state': ac.state.name,
                        'altitude': ac.altitude,
                        'airspeed': ac.airspeed
                    })
        
        return result
    
    def _matches_frequency_context(self, ac: AircraftTrackingData, freq_type: str) -> bool:
        """Check if aircraft matches the frequency context filter."""
        if freq_type == 'ground':
            # Ground: on_ground, speed < 40kts
            return ac.on_ground and ac.airspeed < 40
        
        elif freq_type == 'tower':
            # Tower: on_ground fast (takeoff/landing roll) OR low altitude
            return (ac.on_ground and ac.airspeed >= 40) or (not ac.on_ground and ac.altitude < 3000)
        
        elif freq_type == 'approach':
            # Approach: airborne, below 10000ft, descending
            return not ac.on_ground and ac.altitude < 10000 and ac.vertical_speed < 0
        
        elif freq_type == 'center':
            # Center: high altitude
            return not ac.on_ground and ac.altitude >= 10000
        
        return True  # Default: show all
    
    def _spawn_enhanced_mock(self, own_lat, own_lon, own_alt, now):
        """Spawn a realistic mock aircraft near ownship."""
        # Airline codes with more variety
        airlines = ['CCA', 'CES', 'CSN', 'CHH', 'CXA', 'CQN', 'UAL', 'DAL', 'AAL', 'SWA', 'BAW', 'DLH']
        callsign = f"{random.choice(airlines)}{random.randint(100, 9999)}"
        
        # Ensure no duplicate callsigns
        if callsign in self._mock_aircraft:
            return
        
        # Aircraft types for variety
        aircraft_types = ['Flyover', 'Parallel', 'Approach', 'Departure', 'Hold']
        atype = random.choice(aircraft_types)
        
        params = {}
        if atype == 'Flyover':
            # Aircraft passing through area
            angle = random.uniform(0, 2 * math.pi)
            dist = 0.12  # ~7nm start distance
            params = {
                'start_lat': own_lat + math.sin(angle) * dist,
                'start_lon': own_lon + math.cos(angle) * dist,
                'hdg': (math.degrees(angle) + 180) % 360,  # Heading toward center
                'alt': own_alt + random.randint(-3000, 5000),
                'speed': random.randint(280, 350)
            }
        elif atype == 'Parallel':
            # Aircraft on parallel track
            offset = random.choice([-0.05, 0.05])  # ~3nm offset
            params = {
                'lat_offset': offset,
                'lon_offset': 0,
                'hdg': random.randint(0, 360),
                'alt': own_alt + random.choice([-1000, 1000, 2000]),
                'speed': random.randint(250, 320)
            }
        elif atype in ['Approach', 'Departure']:
            # Aircraft climbing or descending
            angle = random.uniform(0, 2 * math.pi)
            params = {
                'start_lat': own_lat + math.sin(angle) * 0.08,
                'start_lon': own_lon + math.cos(angle) * 0.08,
                'hdg': random.randint(0, 360),
                'alt': 3000 if atype == 'Approach' else 1500,
                'vs': -800 if atype == 'Approach' else 1500,
                'speed': 180 if atype == 'Approach' else 200
            }
        else:  # Hold
            params = {
                'center_lat': own_lat + random.uniform(-0.06, 0.06),
                'center_lon': own_lon + random.uniform(-0.06, 0.06),
                'phase': random.uniform(0, 2 * math.pi),
                'alt': own_alt + random.randint(-2000, 2000),
                'speed': 220
            }
        
        self._mock_aircraft[callsign] = {
            'type': atype,
            'start_time': now,
            'params': params
        }
        print(f"TrafficStateManager: Spawned enhanced mock {callsign} ({atype})")
    
    def _update_enhanced_mock(self, own_lat, own_lon, now):
        """Update enhanced mock aircraft positions."""
        dead_aircraft = []
        
        for callsign, data in list(self._mock_aircraft.items()):
            age = now - data['start_time']
            p = data['params']
            atype = data['type']
            
            try:
                if atype == 'Flyover':
                    # Linear movement
                    speed_deg_per_sec = p['speed'] / 3600 / 60  # Approx deg/sec
                    hdg_rad = math.radians(p['hdg'])
                    lat = p['start_lat'] + math.sin(hdg_rad) * speed_deg_per_sec * age
                    lon = p['start_lon'] + math.cos(hdg_rad) * speed_deg_per_sec * age
                    
                    # Remove if too far
                    if abs(lat - own_lat) > 0.25 or abs(lon - own_lon) > 0.25:
                        dead_aircraft.append(callsign)
                        continue
                    
                    self.update_aircraft(callsign, {
                        'latitude': lat, 'longitude': lon, 'altitude': p['alt'],
                        'heading': p['hdg'], 'airspeed': p['speed'],
                        'vertical_speed': 0, 'on_ground': False
                    })
                    
                elif atype == 'Parallel':
                    lat = own_lat + p['lat_offset']
                    lon = own_lon + p['lon_offset']
                    
                    self.update_aircraft(callsign, {
                        'latitude': lat, 'longitude': lon, 'altitude': p['alt'],
                        'heading': p['hdg'], 'airspeed': p['speed'],
                        'vertical_speed': 0, 'on_ground': False
                    })
                    
                    # Remove after 90s
                    if age > 90:
                        dead_aircraft.append(callsign)
                        
                elif atype in ['Approach', 'Departure']:
                    vs = p.get('vs', 0)
                    alt = p['alt'] + (vs * age / 60)  # altitude change over time
                    
                    # Movement
                    hdg_rad = math.radians(p['hdg'])
                    speed_deg = p['speed'] / 3600 / 60 * age
                    lat = p['start_lat'] + math.sin(hdg_rad) * speed_deg
                    lon = p['start_lon'] + math.cos(hdg_rad) * speed_deg
                    
                    if alt < 0 or alt > 45000 or abs(lat - own_lat) > 0.3:
                        dead_aircraft.append(callsign)
                        continue
                    
                    self.update_aircraft(callsign, {
                        'latitude': lat, 'longitude': lon, 'altitude': max(0, alt),
                        'heading': p['hdg'], 'airspeed': p['speed'],
                        'vertical_speed': vs, 'on_ground': alt < 100
                    })
                    
                else:  # Hold pattern
                    angle = (age * 0.08 + p['phase']) % (2 * math.pi)
                    radius = 0.03
                    lat = p['center_lat'] + math.sin(angle) * radius
                    lon = p['center_lon'] + math.cos(angle) * radius
                    hdg = (math.degrees(angle) + 90) % 360
                    
                    self.update_aircraft(callsign, {
                        'latitude': lat, 'longitude': lon, 'altitude': p['alt'],
                        'heading': hdg, 'airspeed': p['speed'],
                        'vertical_speed': 0, 'on_ground': False
                    })
                    
                    if age > 180:  # 3 minutes max
                        dead_aircraft.append(callsign)
                        
            except Exception as e:
                print(f"TrafficStateManager: Error updating {callsign}: {e}")
                dead_aircraft.append(callsign)
        
        # Cleanup
        for cs in dead_aircraft:
            if cs in self._mock_aircraft:
                del self._mock_aircraft[cs]
            with self.lock:
                if cs in self.aircraft:
                    del self.aircraft[cs]
