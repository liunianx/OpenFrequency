import heapq
import math

import networkx as nx

# stand 节点接入滑行网络的最近 taxi_node 距离阈值（米），与
# osm_ground_service._attach_parking_to_taxi_network 保持一致
STAND_ATTACH_MAX_M = 120.0

_COMPASS_8 = ["north", "north-east", "east", "south-east",
              "south", "south-west", "west", "north-west"]


def _compass_point(bearing_deg) -> str:
    """把角度（度，0=北）转成八点罗盘文字，用于 pushback 朝向指令。"""
    try:
        deg = float(bearing_deg) % 360.0
    except (TypeError, ValueError):
        return "north"
    return _COMPASS_8[int((deg + 22.5) // 45.0) % 8]


class TaxiRouter:
    def __init__(self, ground_service):
        self.ground_service = ground_service
        self.graph = nx.Graph()
        self.airport_icao = None
        self.layout = None

    def build_graph_for_airport(self, airport_icao):
        print(f"TaxiRouter: Building taxi graph for {airport_icao}")
        self.graph = nx.Graph()
        self.airport_icao = airport_icao
        self.layout = self.ground_service.get_airport_layout(airport_icao)
        if not self.layout:
            print(f"TaxiRouter: No ground layout available for {airport_icao}")
            return None

        node_lookup = {}
        for node in self.layout.get("taxi_nodes", []):
            node_id = str(node.get("id"))
            node_lookup[node_id] = node
            self.graph.add_node(
                node_id,
                lat=node.get("lat"),
                lon=node.get("lon"),
                usage=node.get("usage", "both"),
                hotspot=False,
                stand_name="",
            )

        for stand in self.layout.get("startup_locations", []):
            stand_name = stand.get("gate_id") or stand.get("name") or ""
            nearest = self.find_nearest_node(stand.get("lat"), stand.get("lon"))
            if nearest and nearest in self.graph.nodes:
                self.graph.nodes[nearest]["stand_name"] = stand_name

        for edge in self.layout.get("taxi_edges", []):
            start_id = str(edge.get("start"))
            end_id = str(edge.get("end"))
            if start_id not in self.graph or end_id not in self.graph:
                continue

            distance_m = self._distance_m(
                self.graph.nodes[start_id]["lat"],
                self.graph.nodes[start_id]["lon"],
                self.graph.nodes[end_id]["lat"],
                self.graph.nodes[end_id]["lon"],
            )
            kind = edge.get("kind", "taxiway")
            name = edge.get("name", "")
            runway_names = set()
            if kind == "runway":
                parts = [part.strip() for part in name.replace("runway", "").split("/") if part.strip()]
                runway_names.update(parts)

            self.graph.add_edge(
                start_id,
                end_id,
                distance_m=distance_m,
                direction=edge.get("direction", "twoway"),
                kind=kind,
                name=name,
                width_m=float(edge.get("width_m") or 0.0),
                surface=edge.get("surface", ""),
                runway_names=runway_names,
                runway_crossing=(kind == "runway"),
            )

        self._attach_stands()
        self._mark_hotspots()
        print(
            f"TaxiRouter: Loaded {self.graph.number_of_nodes()} nodes and "
            f"{self.graph.number_of_edges()} edges for {airport_icao}"
        )
        return self.layout

    def find_nearest_node(self, lat, lon, usage=None):
        if lat is None or lon is None or self.graph.number_of_nodes() == 0:
            return None
        candidates = []
        for node_id, data in self.graph.nodes(data=True):
            if usage and data.get("usage") != usage:
                continue
            candidates.append((self._distance_m(lat, lon, data.get("lat"), data.get("lon")), node_id))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def suggest_taxi_route(self, airport_icao, aircraft_position, preferred_runways=None, aircraft_size="medium", low_visibility=False):
        if airport_icao != self.airport_icao or self.graph.number_of_nodes() == 0:
            self.build_graph_for_airport(airport_icao)
        if self.graph.number_of_nodes() == 0:
            return None

        preferred_runways = [str(item).upper() for item in (preferred_runways or []) if item]
        start_node = self.find_nearest_node(aircraft_position.get("lat"), aircraft_position.get("lon"))
        if not start_node:
            return None

        end_candidates = self._find_runway_entry_nodes(preferred_runways)
        if not end_candidates:
            return None

        best_route = None
        for end_node in end_candidates:
            route = self.find_path(
                start_node,
                end_node,
                aircraft_size=aircraft_size,
                low_visibility=low_visibility,
            )
            if not route:
                continue
            if best_route is None or route["cost"] < best_route["cost"]:
                best_route = route

        if not best_route:
            return None

        taxiways = []
        runway_crossings = 0
        for start_id, end_id in zip(best_route["path"], best_route["path"][1:]):
            edge = self.graph.edges[start_id, end_id]
            name = edge.get("name", "")
            if edge.get("runway_crossing"):
                runway_crossings += 1
            if name and name not in taxiways and edge.get("kind") != "runway":
                taxiways.append(name)

        return {
            "path": best_route["path"],
            "taxiways": taxiways,
            "cost": round(best_route["cost"], 1),
            "runway_crossings": runway_crossings,
            "end_node": best_route["path"][-1],
            "target_runway": self._runway_for_node(best_route["path"][-1]),
        }

    def find_path(self, start, end, aircraft_size="medium", low_visibility=False):
        if start not in self.graph or end not in self.graph:
            return None

        queue = [(0.0, start, None, [start])]
        best_cost = {(start, None): 0.0}

        while queue:
            cost, current, previous, path = heapq.heappop(queue)
            if current == end:
                return {"path": path, "cost": cost}

            for neighbor in self.graph.neighbors(current):
                if previous is not None and neighbor == previous and len(path) > 2:
                    continue

                edge = self.graph.edges[current, neighbor]
                step_cost = self._edge_cost(previous, current, neighbor, edge, aircraft_size, low_visibility)
                new_cost = cost + step_cost
                state = (neighbor, current)
                if new_cost >= best_cost.get(state, float("inf")):
                    continue
                best_cost[state] = new_cost
                heapq.heappush(queue, (new_cost, neighbor, current, path + [neighbor]))

        return None

    def _find_runway_entry_nodes(self, preferred_runways):
        candidates = []
        for start_id, end_id, edge in self.graph.edges(data=True):
            if edge.get("kind") != "runway":
                continue
            runway_names = {name.upper() for name in edge.get("runway_names", set())}
            if preferred_runways and runway_names:
                if not any(rwy in runway_names or any(rwy in item for item in runway_names) for rwy in preferred_runways):
                    continue
            candidates.extend([start_id, end_id])
        return list(dict.fromkeys(candidates))

    def _runway_for_node(self, node_id):
        runway_names = []
        if node_id not in self.graph:
            return ""
        for neighbor in self.graph.neighbors(node_id):
            edge = self.graph.edges[node_id, neighbor]
            if edge.get("kind") != "runway":
                continue
            for runway_name in edge.get("runway_names", set()):
                if runway_name and runway_name not in runway_names:
                    runway_names.append(runway_name)
        return " / ".join(runway_names)

    def _edge_cost(self, previous, current, neighbor, edge, aircraft_size, low_visibility):
        base = float(edge.get("distance_m", 0.0))
        runway_penalty = 4000.0 if edge.get("runway_crossing") else 0.0
        hotspot_penalty = 600.0 if self.graph.nodes[current].get("hotspot") else 0.0
        complexity_penalty = max(0, self.graph.degree(current) - 2) * 45.0
        turn_penalty = self._turn_penalty(previous, current, neighbor)

        edge_name = (edge.get("name") or "").lower()
        kind = (edge.get("kind") or "").lower()
        large_aircraft_penalty = 0.0
        if aircraft_size in {"heavy", "large"}:
            if kind in {"taxiway_link", "apron_link"}:
                large_aircraft_penalty += 150.0
            if turn_penalty > 160.0:
                large_aircraft_penalty += 250.0
            width_m = float(edge.get("width_m") or 0.0)
            if width_m and width_m < 18.0:
                large_aircraft_penalty += 300.0
        if "hotspot" in edge_name:
            hotspot_penalty += 300.0

        low_vis_penalty = 0.0
        if low_visibility:
            low_vis_penalty += complexity_penalty * 0.8
            low_vis_penalty += hotspot_penalty * 0.5
            if turn_penalty > 0:
                low_vis_penalty += 100.0

        return base + runway_penalty + hotspot_penalty + complexity_penalty + turn_penalty + large_aircraft_penalty + low_vis_penalty

    def _turn_penalty(self, previous, current, neighbor):
        if previous is None:
            return 0.0
        p1 = self.graph.nodes[previous]
        p2 = self.graph.nodes[current]
        p3 = self.graph.nodes[neighbor]
        bearing1 = self._bearing(p1["lat"], p1["lon"], p2["lat"], p2["lon"])
        bearing2 = self._bearing(p2["lat"], p2["lon"], p3["lat"], p3["lon"])
        delta = abs((bearing2 - bearing1 + 180.0) % 360.0 - 180.0)
        return delta * 4.0

    def _attach_stands(self):
        """把停机位作为 stand:{gate_id} 节点接入滑行图（G3）。

        apt.dat 源的 startup_locations 只是元数据、stand 不是图节点；
        OSM 源在 osm_ground_service._attach_parking_to_taxi_network 里已显式建
        stand 节点。所有源统一走这里，suggest_taxi_in_route 才有稳定终点。
        最近 taxi_node 超过 STAND_ATTACH_MAX_M 的 stand 不连线（视为不可滑入）。
        """
        if not self.layout:
            return
        taxi_nodes = [n for n in self.graph.nodes]
        for stand in self.layout.get("startup_locations", []) or []:
            lat, lon = stand.get("lat"), stand.get("lon")
            if lat is None or lon is None:
                continue
            gate_id = (stand.get("gate_id") or stand.get("name") or "").strip()
            if not gate_id:
                continue
            stand_id = f"stand:{gate_id}"
            if stand_id not in self.graph.nodes:
                self.graph.add_node(
                    stand_id, lat=lat, lon=lon, usage="gate",
                    hotspot=False, stand_name=gate_id,
                )
            if not taxi_nodes:
                continue
            nearest = min(
                taxi_nodes,
                key=lambda node_id: self._distance_m(
                    lat, lon,
                    self.graph.nodes[node_id].get("lat"),
                    self.graph.nodes[node_id].get("lon"),
                ),
            )
            if self._distance_m(lat, lon,
                                self.graph.nodes[nearest].get("lat"),
                                self.graph.nodes[nearest].get("lon")) <= STAND_ATTACH_MAX_M:
                self.graph.add_edge(
                    stand_id, nearest,
                    distance_m=self._distance_m(
                        lat, lon,
                        self.graph.nodes[nearest].get("lat"),
                        self.graph.nodes[nearest].get("lon"),
                    ),
                    direction="twoway",
                    kind="apron_link",
                    name=gate_id,
                    width_m=18.0,
                    surface="paved",
                    runway_names=set(),
                    runway_crossing=False,
                )

    def suggest_taxi_in_route(self, airport_icao, aircraft_position, stand_ident):
        """进港滑行：跑道邻接节点 → 指定停机位（A3）。

        起点取距飞机最近的 runway 邻接节点（runway_links>0，复用 _mark_hotspots 的
        判定，不另写几何算法）；终点为 stand:{stand_ident}。返回结构与
        suggest_taxi_route 对齐。
        """
        if airport_icao != self.airport_icao or self.graph.number_of_nodes() == 0:
            self.build_graph_for_airport(airport_icao)
        if self.graph.number_of_nodes() == 0:
            return None

        stand_id = f"stand:{stand_ident}" if stand_ident else None
        if not stand_id or stand_id not in self.graph:
            return None

        pos = aircraft_position or {}
        if pos.get("lat") is None or pos.get("lon") is None:
            return None

        runway_nodes = [node_id for node_id, data in self.graph.nodes(data=True)
                        if data.get("runway_links", 0) > 0]
        if not runway_nodes:
            return None
        start_node = min(
            runway_nodes,
            key=lambda node_id: self._distance_m(
                pos["lat"], pos["lon"],
                self.graph.nodes[node_id].get("lat"),
                self.graph.nodes[node_id].get("lon"),
            ),
        )

        route = self.find_path(start_node, stand_id)
        if not route:
            return None

        taxiways = []
        runway_crossings = 0
        for start_id, end_id in zip(route["path"], route["path"][1:]):
            edge = self.graph.edges[start_id, end_id]
            name = edge.get("name", "")
            if edge.get("runway_crossing"):
                runway_crossings += 1
            if name and name not in taxiways and edge.get("kind") != "runway":
                taxiways.append(name)

        return {
            "path": route["path"],
            "taxiways": taxiways,
            "cost": round(route["cost"], 1),
            "runway_crossings": runway_crossings,
            "end_node": stand_id,
            "stand": stand_ident,
        }

    def suggest_pushback_direction(self, stand, airport_icao=None):
        """推出朝向（A2）：优先用停机位 heading（apt.dat 1300 行有），
        OSM 源没有朝向时退化为最近滑行道边的方向。返回 'west' / 'north-east' 等，
        取不到返回 None。"""
        stand = stand or {}
        heading = stand.get("heading")
        if heading not in (None, "", 0, 0.0):
            return _compass_point(heading)
        lat, lon = stand.get("lat"), stand.get("lon")
        if lat is None or lon is None:
            return None
        if airport_icao and (airport_icao != self.airport_icao or self.graph.number_of_nodes() == 0):
            self.build_graph_for_airport(airport_icao)
        if self.graph.number_of_nodes() == 0:
            return None
        nearest = self.find_nearest_node(lat, lon)
        if not nearest:
            return None
        node = self.graph.nodes[nearest]
        if node.get("lat") is None or node.get("lon") is None:
            return None
        # 退化方向：停机位指向最近滑行道节点的方位
        return _compass_point(self._bearing(lat, lon, node["lat"], node["lon"]))

    def _mark_hotspots(self):
        for node_id in self.graph.nodes:
            degree = self.graph.degree(node_id)
            runway_links = 0
            for neighbor in self.graph.neighbors(node_id):
                edge = self.graph.edges[node_id, neighbor]
                if edge.get("kind") == "runway":
                    runway_links += 1
            self.graph.nodes[node_id]["runway_links"] = runway_links
            self.graph.nodes[node_id]["hotspot"] = degree >= 4 or runway_links > 0

    @staticmethod
    def _distance_m(lat1, lon1, lat2, lon2):
        radius_m = 6371000.0
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _bearing(lat1, lon1, lat2, lon2):
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        x = math.sin(dlambda) * math.cos(phi2)
        y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
        return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
