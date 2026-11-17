#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
banc_coverage_modal.py - Interactive Coverage Map & Cloud Layout Repository Modal

Used by both ApexBOSCreator.py (Layout Studio) and View_ApexBOS.220.py (Dispatcher Viewer).
Displays:
  - Interactive coverage map of all Banc Cloud layouts (center GPS + radius circles).
  - Searchable/scrollable list of remote layouts with availability badges, GPS, and radius.
  - Prohibits downloading layouts marked unavailable.
  - Allows loading a layout directly into Engine/Viewer or downloading into Creator.
"""

import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pygame

COLOR_EDIT_ACCENT = (255, 185, 40)
COLOR_ERROR_RED = (245, 50, 50)


class BancCoverageMapModal:
    """
    Modal overlay for Banc Cloud remote layout management and coverage map display.
    """

    def __init__(
        self,
        tile_mgr: Any,
        client: Any,
        on_download_and_open_cb: Callable[..., None],
        on_upload_current_cb: Optional[Callable[[], None]] = None,
        on_close_cb: Optional[Callable[[], None]] = None,
        zoom: int = 7,
        is_viewer_mode: bool = False,
        fetch_layouts_fallback_cb: Optional[Callable[[], List[dict]]] = None,
    ):
        self.tile_mgr = tile_mgr
        self.client = client
        self.on_download_and_open_cb = on_download_and_open_cb
        self.on_upload_current_cb = on_upload_current_cb
        self.on_close_cb = on_close_cb
        self.zoom = zoom
        self.is_viewer_mode = is_viewer_mode
        self.fetch_layouts_fallback_cb = fetch_layouts_fallback_cb

        self.center_lat = 39.8283
        self.center_lon = -98.5795
        self.layer = "sat"
        self.layouts: List[Dict[str, Any]] = []
        self.selected_idx: Optional[int] = 0
        self.hover_idx: Optional[int] = None
        self.status_toast: str = ""
        self.status_toast_ts: float = 0.0
        self.scroll_y: float = 0.0
        self.max_scroll: float = 0.0
        self.modal_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.list_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.map_rect: pygame.Rect = pygame.Rect(0, 0, 0, 0)
        self.btn_rects: Dict[str, pygame.Rect] = {}
        self.card_rects: List[pygame.Rect] = []
        self.is_panning: bool = False
        self.pan_has_moved: bool = False
        self.pan_start_mouse = (0, 0)
        self.pan_start_world_px = (0.0, 0.0)
        self._last_click_ts: float = 0.0
        self.refresh()

    def set_toast(self, msg: str):
        self.status_toast = msg
        self.status_toast_ts = time.time()

    def refresh(self):
        fetched = None
        if self.client:
            try:
                fetched = self.client.list_layouts_sync()
            except Exception as exc:
                fetched = None

        if fetched is None and self.fetch_layouts_fallback_cb:
            try:
                fetched = self.fetch_layouts_fallback_cb()
            except Exception:
                fetched = None

        if fetched is not None:
            self.layouts = fetched
            self.set_toast(f"Retrieved {len(self.layouts)} layout(s) from Banc Cloud.")
        else:
            self.layouts = []
            self.set_toast("Banc Cloud error: Could not retrieve layout list.")

        if self.layouts:
            if self.selected_idx is None or self.selected_idx >= len(self.layouts):
                self.selected_idx = 0
            gps_pts = [
                (it["center_lat"], it["center_lon"])
                for it in self.layouts
                if it.get("center_lat") is not None and it.get("center_lon") is not None
            ]
            if gps_pts:
                self.center_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                self.center_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
        else:
            self.selected_idx = None

    def center_on_layout(self, idx: int):
        if 0 <= idx < len(self.layouts):
            it = self.layouts[idx]
            if it.get("center_lat") is not None and it.get("center_lon") is not None:
                self.center_lat = float(it["center_lat"])
                self.center_lon = float(it["center_lon"])

    def zoom_at_point(self, mx: int, my: int, delta_z: int):
        new_z = max(3, min(19, self.zoom + delta_z))
        if new_z == self.zoom:
            return
        if self.map_rect.width > 0 and self.map_rect.collidepoint(mx, my):
            try:
                cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
                dx = mx - self.map_rect.centerx
                dy = my - self.map_rect.centery
                hover_lat, hover_lon = self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)
                self.zoom = new_z
                new_hx, new_hy = self.tile_mgr.latlon_to_world_px(hover_lat, hover_lon, self.zoom)
                self.center_lat, self.center_lon = self.tile_mgr.world_px_to_latlon(new_hx - dx, new_hy - dy, self.zoom)
            except Exception:
                self.zoom = new_z
        else:
            self.zoom = new_z

    def screen_to_latlon(self, mx: int, my: int) -> Tuple[float, float]:
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
        dx = mx - self.map_rect.centerx
        dy = my - self.map_rect.centery
        return self.tile_mgr.world_px_to_latlon(cx_px + dx, cy_px + dy, self.zoom)

    def download_selected(self):
        if self.selected_idx is None or self.selected_idx < 0 or self.selected_idx >= len(self.layouts):
            self.set_toast("No layout selected.")
            return
        item = self.layouts[self.selected_idx]
        name = item.get("layout_name") or item.get("name")
        if not item.get("is_available"):
            self.set_toast(f"Layout '{name}' is marked UNAVAILABLE and cannot be downloaded.")
            return

        if self.is_viewer_mode:
            self.set_toast(f"Loading '{name}' into Engine...")
            if self.on_download_and_open_cb:
                try:
                    self.on_download_and_open_cb(name)
                except Exception as exc:
                    self.set_toast(f"Load failed: {exc}")
        else:
            if not self.client:
                self.set_toast("Banc Cloud error: Banc client not configured.")
                return
            self.set_toast(f"Downloading '{name}' from Banc Cloud...")
            try:
                data = self.client.download_layout_sync(name)
                if not data:
                    self.set_toast(f"Failed to download '{name}' (empty response).")
                    return
                if self.on_download_and_open_cb:
                    self.on_download_and_open_cb(data, name)
            except Exception as exc:
                self.set_toast(f"Download failed: {exc}")

    def draw(self, screen: pygame.Surface, font_sm, font_md, font_sm_bold):
        sw, sh = screen.get_size()
        mw = min(sw - 40, 1180)
        mh = min(sh - 40, 720)
        mx = (sw - mw) // 2
        my = (sh - mh) // 2
        self.modal_rect = pygame.Rect(mx, my, mw, mh)

        overlay = pygame.Surface((sw, sh), pygame.SRCALPHA)
        overlay.fill((4, 8, 14, 210))
        screen.blit(overlay, (0, 0))

        pygame.draw.rect(screen, (16, 22, 32), self.modal_rect, border_radius=8)
        pygame.draw.rect(screen, (52, 75, 110), self.modal_rect, 2, border_radius=8)

        # Header
        hdr_rect = pygame.Rect(mx, my, mw, 48)
        pygame.draw.rect(screen, (24, 32, 46), hdr_rect, border_top_left_radius=8, border_top_right_radius=8)
        pygame.draw.line(screen, (45, 65, 95), (mx, my + 48), (mx + mw, my + 48), 1)

        title_surf = font_md.render("BANC CLOUD LAYOUT COVERAGE & REMOTE REPOSITORY", True, (255, 255, 255))
        screen.blit(title_surf, (mx + 16, my + 8))
        h_host = getattr(self.client, "host", "127.0.0.1")
        h_port = getattr(self.client, "port", 23009)
        if self.is_viewer_mode:
            sub_text = f"Cloud Server: {h_host}:{h_port}  |  Pick layout to load into running Engine  |  Coverage map displays layout GPS radius"
        else:
            sub_text = f"Cloud Server: {h_host}:{h_port}  |  Pick layout to download & open as local  |  Coverage map displays layout GPS radius"
        sub_surf = font_sm.render(sub_text, True, (130, 160, 195))
        screen.blit(sub_surf, (mx + 16, my + 28))

        close_btn_rect = pygame.Rect(mx + mw - 36, my + 10, 26, 26)
        self.btn_rects["close"] = close_btn_rect
        pygame.draw.rect(screen, (70, 30, 35), close_btn_rect, border_radius=4)
        pygame.draw.rect(screen, (140, 60, 65), close_btn_rect, 1, border_radius=4)
        x_surf = font_sm_bold.render("X", True, (255, 210, 210))
        screen.blit(x_surf, (close_btn_rect.centerx - x_surf.get_width() // 2, close_btn_rect.centery - x_surf.get_height() // 2))

        # Main Layout Split: Left List (370px) + Right Map
        list_w = 370
        map_w = mw - list_w - 28
        body_h = mh - 48 - 14
        self.list_rect = pygame.Rect(mx + 12, my + 54, list_w, body_h - 100)
        self.map_rect = pygame.Rect(mx + 12 + list_w + 10, my + 54, map_w, body_h - 40)

        # Draw Left Panel (Layout List & Actions)
        screen.blit(font_sm_bold.render(f"Remote Layouts ({len(self.layouts)}):", True, COLOR_EDIT_ACCENT), (self.list_rect.x, self.list_rect.y - 18))

        list_box = pygame.Rect(self.list_rect.x, self.list_rect.y, self.list_rect.width, self.list_rect.height)
        pygame.draw.rect(screen, (20, 26, 38), list_box, border_radius=4)
        pygame.draw.rect(screen, (45, 60, 85), list_box, 1, border_radius=4)

        card_h = 68
        card_gap = 6
        total_content_h = max(1, len(self.layouts) * (card_h + card_gap))
        self.max_scroll = max(0.0, float(total_content_h - self.list_rect.height + 10))

        screen.set_clip(self.list_rect)
        self.card_rects = []
        for idx, it in enumerate(self.layouts):
            cx = self.list_rect.x + 6
            cy = int(self.list_rect.y + 6 + idx * (card_h + card_gap) - self.scroll_y)
            card_w = self.list_rect.width - 16
            crect = pygame.Rect(cx, cy, card_w, card_h)
            self.card_rects.append(crect)

            if cy + card_h < self.list_rect.y or cy > self.list_rect.bottom:
                continue

            is_sel = self.selected_idx == idx
            is_hov = self.hover_idx == idx
            bg = (38, 52, 74) if is_sel else ((28, 38, 54) if is_hov else (22, 30, 42))
            border_c = COLOR_EDIT_ACCENT if is_sel else ((70, 95, 135) if is_hov else (38, 50, 70))
            pygame.draw.rect(screen, bg, crect, border_radius=4)
            pygame.draw.rect(screen, border_c, crect, 2 if is_sel else 1, border_radius=4)

            # Name
            name_txt = str(it.get("layout_name") or it.get("name") or "unknown")
            n_surf = font_sm_bold.render(name_txt[:26], True, (255, 255, 255) if is_sel else (225, 235, 250))
            screen.blit(n_surf, (cx + 8, cy + 6))

            # Availability Badge
            is_avail = bool(it.get("is_available"))
            badge_txt = "AVAILABLE" if is_avail else "UNAVAILABLE"
            badge_color = (40, 210, 90) if is_avail else (235, 60, 60)
            badge_surf = font_sm_bold.render(badge_txt, True, badge_color)
            bw, bh = badge_surf.get_width() + 6, badge_surf.get_height() + 2
            bx = cx + card_w - bw - 6
            by = cy + 6
            pygame.draw.rect(screen, (15, 20, 28, 200), (bx, by, bw, bh), border_radius=3)
            pygame.draw.rect(screen, badge_color, (bx, by, bw, bh), 1, border_radius=3)
            screen.blit(badge_surf, (bx + 3, by + 1))

            # GPS & Radius
            lat = it.get("center_lat")
            lon = it.get("center_lon")
            rad = it.get("radius_km")
            if lat is not None and lon is not None:
                gps_str = f"GPS: {lat:.4f}, {lon:.4f}  |  Rad: {rad:.1f}km ({rad*0.621371:.1f}mi)" if rad is not None else f"GPS: {lat:.4f}, {lon:.4f}"
            else:
                gps_str = "No GPS Information"
            g_surf = font_sm.render(gps_str, True, (135, 175, 220) if is_sel else (125, 140, 165))
            screen.blit(g_surf, (cx + 8, cy + 28))

            # Creator/Updated info
            up_by = it.get("uploaded_by") or "user"
            raw_stamp = it.get("stamp_updated") or it.get("stamp_created")
            up_time_str = ""
            if isinstance(raw_stamp, (int, float)) and raw_stamp > 0:
                try:
                    up_time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(raw_stamp))
                except Exception:
                    up_time_str = str(raw_stamp)[:16]
            elif raw_stamp:
                up_time_str = str(raw_stamp)[:16]

            t_str = f"By: {up_by}" + (f"  |  {up_time_str}" if up_time_str else "")
            t_surf = font_sm.render(t_str, True, (100, 120, 145))
            screen.blit(t_surf, (cx + 8, cy + 46))

        screen.set_clip(None)

        # Left Action Buttons Under List
        act_y = self.list_rect.bottom + 12
        sel_is_avail = (
            self.selected_idx is not None
            and 0 <= self.selected_idx < len(self.layouts)
            and self.layouts[self.selected_idx].get("is_available")
        )

        if self.is_viewer_mode or self.on_upload_current_cb is None:
            # Full width button for Load Layout
            btn_dl = pygame.Rect(self.list_rect.x, act_y, list_w, 32)
            self.btn_rects["download_open"] = btn_dl
            dl_bg = (35, 120, 75) if sel_is_avail else (45, 55, 65)
            pygame.draw.rect(screen, dl_bg, btn_dl, border_radius=4)
            pygame.draw.rect(screen, (55, 165, 105) if sel_is_avail else (70, 80, 90), btn_dl, 1, border_radius=4)
            dl_txt = font_sm_bold.render("Load Layout (Enter)", True, (255, 255, 255) if sel_is_avail else (140, 150, 160))
            screen.blit(dl_txt, (btn_dl.centerx - dl_txt.get_width() // 2, btn_dl.centery - dl_txt.get_height() // 2))
        else:
            btn_w_half = (list_w - 8) // 2
            btn_dl = pygame.Rect(self.list_rect.x, act_y, btn_w_half, 32)
            self.btn_rects["download_open"] = btn_dl
            dl_bg = (35, 120, 75) if sel_is_avail else (45, 55, 65)
            pygame.draw.rect(screen, dl_bg, btn_dl, border_radius=4)
            pygame.draw.rect(screen, (55, 165, 105) if sel_is_avail else (70, 80, 90), btn_dl, 1, border_radius=4)
            dl_txt = font_sm_bold.render("Download & Open", True, (255, 255, 255) if sel_is_avail else (140, 150, 160))
            screen.blit(dl_txt, (btn_dl.centerx - dl_txt.get_width() // 2, btn_dl.centery - dl_txt.get_height() // 2))

            btn_up = pygame.Rect(self.list_rect.x + btn_w_half + 8, act_y, btn_w_half, 32)
            self.btn_rects["upload_current"] = btn_up
            pygame.draw.rect(screen, (45, 85, 140), btn_up, border_radius=4)
            pygame.draw.rect(screen, (70, 125, 200), btn_up, 1, border_radius=4)
            up_txt = font_sm_bold.render("Upload Current", True, (255, 255, 255))
            screen.blit(up_txt, (btn_up.centerx - up_txt.get_width() // 2, btn_up.centery - up_txt.get_height() // 2))

        btn_w_half = (list_w - 8) // 2
        btn_ref = pygame.Rect(self.list_rect.x, act_y + 38, btn_w_half, 28)
        self.btn_rects["refresh"] = btn_ref
        pygame.draw.rect(screen, (42, 60, 85), btn_ref, border_radius=4)
        pygame.draw.rect(screen, (65, 90, 125), btn_ref, 1, border_radius=4)
        ref_txt = font_sm_bold.render("Refresh List", True, (210, 225, 245))
        screen.blit(ref_txt, (btn_ref.centerx - ref_txt.get_width() // 2, btn_ref.centery - ref_txt.get_height() // 2))

        btn_canc = pygame.Rect(self.list_rect.x + btn_w_half + 8, act_y + 38, btn_w_half, 28)
        self.btn_rects["cancel"] = btn_canc
        pygame.draw.rect(screen, (65, 35, 42), btn_canc, border_radius=4)
        pygame.draw.rect(screen, (115, 60, 70), btn_canc, 1, border_radius=4)
        canc_txt = font_sm_bold.render("Close", True, (245, 215, 220))
        screen.blit(canc_txt, (btn_canc.centerx - canc_txt.get_width() // 2, btn_canc.centery - canc_txt.get_height() // 2))

        # Status Toast Bar
        if self.status_toast:
            st_color = (
                COLOR_ERROR_RED
                if "error" in self.status_toast.lower() or "unavail" in self.status_toast.lower()
                else COLOR_EDIT_ACCENT
            )
            screen.blit(font_sm.render(self.status_toast[:55], True, st_color), (self.list_rect.x, act_y + 72))

        # Draw Right Panel (Coverage Map)
        mode_lbl = "SATELLITE" if self.layer == "sat" else "STREET MAP"
        map_title = f"CLOUD LAYOUT COVERAGE MAP [{mode_lbl} | Zoom:{self.zoom}] — Drag to Pan | Scroll to Zoom | Click Pin/Circle to Select"
        screen.blit(font_sm_bold.render(map_title, True, (0, 225, 245)), (self.map_rect.x, self.map_rect.y - 18))

        map_surf = pygame.Surface((self.map_rect.width, self.map_rect.height))
        map_surf.fill((22, 28, 38))

        z = self.zoom
        cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, z)
        mw_map, mh_map = self.map_rect.width, self.map_rect.height

        min_tx = int(math.floor((cx_px - mw_map / 2.0) / 256.0))
        max_tx = int(math.floor((cx_px + mw_map / 2.0) / 256.0))
        min_ty = int(math.floor((cy_px - mh_map / 2.0) / 256.0))
        max_ty = int(math.floor((cy_px + mh_map / 2.0) / 256.0))
        n_tiles = 2 ** z

        for tx in range(min_tx, max_tx + 1):
            for ty in range(min_ty, max_ty + 1):
                wrapped_tx = tx % n_tiles
                if ty < 0 or ty >= n_tiles:
                    continue
                tile_s = self.tile_mgr.get_tile_surface(z, wrapped_tx, ty, layer=self.layer)
                dx = int(mw_map / 2.0 + (tx * 256.0 - cx_px))
                dy = int(mh_map / 2.0 + (ty * 256.0 - cy_px))
                if tile_s:
                    map_surf.blit(tile_s, (dx, dy))
                else:
                    pygame.draw.rect(map_surf, (34, 44, 58), (dx, dy, 256, 256), 1)

        # Draw Coverage Circles and Center Pins for all layouts
        for idx, it in enumerate(self.layouts):
            lat = it.get("center_lat")
            lon = it.get("center_lon")
            if lat is None or lon is None:
                continue

            ex_px, ey_px = self.tile_mgr.latlon_to_world_px(lat, lon, z)
            px = int(mw_map / 2.0 + (ex_px - cx_px))
            py = int(mh_map / 2.0 + (ey_px - cy_px))

            rad_km = float(it.get("radius_km") or 1.0)
            lat_rad = math.radians(lat)
            meters_per_px = 156543.03392 * math.cos(lat_rad) / (2.0 ** z)
            max_r = max(mw_map, mh_map)
            rad_px = max(8, min(max_r, int((rad_km * 1000.0) / max(0.001, meters_per_px))))

            is_sel = self.selected_idx == idx
            is_avail = bool(it.get("is_available"))

            # Coverage circle
            if is_sel:
                circ_color = (255, 215, 30)
                fill_color = (255, 215, 30, 40)
                circ_w = 3
            elif is_avail:
                circ_color = (0, 230, 180)
                fill_color = (0, 230, 180, 25)
                circ_w = 2
            else:
                circ_color = (235, 75, 75)
                fill_color = (235, 75, 75, 25)
                circ_w = 2

            if -rad_px < px < mw_map + rad_px and -rad_px < py < mh_map + rad_px:
                try:
                    c_surf = pygame.Surface((rad_px * 2 + 4, rad_px * 2 + 4), pygame.SRCALPHA)
                    pygame.draw.circle(c_surf, fill_color, (rad_px + 2, rad_px + 2), rad_px)
                    pygame.draw.circle(c_surf, circ_color, (rad_px + 2, rad_px + 2), rad_px, circ_w)
                    map_surf.blit(c_surf, (px - rad_px - 2, py - rad_px - 2))
                except Exception:
                    pygame.draw.circle(map_surf, circ_color, (px, py), min(rad_px, max_r), circ_w)

            # Center pin & Label
            if 0 <= px < mw_map and 0 <= py < mh_map:
                pin_c = (255, 215, 30) if is_sel else circ_color
                pygame.draw.circle(map_surf, (15, 20, 28), (px, py), 9)
                pygame.draw.circle(map_surf, pin_c, (px, py), 6)
                pygame.draw.circle(map_surf, (255, 255, 255), (px, py), 2)

                lname = str(it.get("layout_name") or it.get("name") or "layout")
                avail_tag = "AVAIL" if is_avail else "UNAVAIL"
                tag_txt = f"{lname} [{avail_tag} | {rad_km:.1f}km]"
                tag_surf = font_sm.render(tag_txt, True, (255, 255, 255) if is_sel else (220, 230, 245))
                tw, th = tag_surf.get_width() + 8, tag_surf.get_height() + 4
                tbox = pygame.Surface((tw, th), pygame.SRCALPHA)
                tbox.fill((16, 24, 34, 230))
                pygame.draw.rect(tbox, pin_c if is_sel else (60, 80, 110), (0, 0, tw, th), 1, border_radius=3)
                tbox.blit(tag_surf, (4, 2))
                map_surf.blit(tbox, (px - tw // 2, py - 14 - th))

        pygame.draw.rect(map_surf, (55, 75, 105), (0, 0, mw_map, mh_map), 2)
        screen.blit(map_surf, self.map_rect.topleft)

        # Map Bottom Control Bar
        mctrl_y = self.map_rect.bottom + 8
        btn_layer = pygame.Rect(self.map_rect.x, mctrl_y, 110, 26)
        self.btn_rects["toggle_layer"] = btn_layer
        pygame.draw.rect(screen, (55, 45, 95), btn_layer, border_radius=3)
        pygame.draw.rect(screen, (85, 70, 145), btn_layer, 1, border_radius=3)
        lyr_txt = font_sm_bold.render("Sat / Street", True, (230, 220, 255))
        screen.blit(lyr_txt, (btn_layer.centerx - lyr_txt.get_width() // 2, btn_layer.centery - lyr_txt.get_height() // 2))

        btn_zin = pygame.Rect(self.map_rect.x + 118, mctrl_y, 65, 26)
        self.btn_rects["zoom_in"] = btn_zin
        pygame.draw.rect(screen, (40, 50, 70), btn_zin, border_radius=3)
        pygame.draw.rect(screen, (60, 75, 105), btn_zin, 1, border_radius=3)
        zin_txt = font_sm_bold.render("Zoom +", True, (220, 235, 255))
        screen.blit(zin_txt, (btn_zin.centerx - zin_txt.get_width() // 2, btn_zin.centery - zin_txt.get_height() // 2))

        btn_zout = pygame.Rect(self.map_rect.x + 191, mctrl_y, 65, 26)
        self.btn_rects["zoom_out"] = btn_zout
        pygame.draw.rect(screen, (40, 50, 70), btn_zout, border_radius=3)
        pygame.draw.rect(screen, (60, 75, 105), btn_zout, 1, border_radius=3)
        zout_txt = font_sm_bold.render("Zoom -", True, (220, 235, 255))
        screen.blit(zout_txt, (btn_zout.centerx - zout_txt.get_width() // 2, btn_zout.centery - zout_txt.get_height() // 2))

        btn_fit = pygame.Rect(self.map_rect.x + 264, mctrl_y, 85, 26)
        self.btn_rects["center_all"] = btn_fit
        pygame.draw.rect(screen, (40, 50, 70), btn_fit, border_radius=3)
        pygame.draw.rect(screen, (60, 75, 105), btn_fit, 1, border_radius=3)
        fit_txt = font_sm_bold.render("Center All", True, (220, 235, 255))
        screen.blit(fit_txt, (btn_fit.centerx - fit_txt.get_width() // 2, btn_fit.centery - fit_txt.get_height() // 2))

    def handle_scroll(self, scroll_y: int, mx: int, my: int):
        """Handles mouse wheel scrolling for either layout list or map zoom."""
        if hasattr(self, "list_rect") and self.list_rect.width > 0 and self.list_rect.collidepoint(mx, my):
            self.scroll_y = max(0.0, min(self.max_scroll, self.scroll_y - scroll_y * 30))
        elif hasattr(self, "map_rect") and self.map_rect.width > 0 and self.map_rect.collidepoint(mx, my):
            self.zoom_at_point(mx, my, 1 if scroll_y > 0 else -1)

    def handle_mouse_wheel(self, event: Any):
        """Compatibility wrapper for pygame mouse wheel event."""
        mx, my = pygame.mouse.get_pos()
        sy = getattr(event, "y", 0)
        self.handle_scroll(sy, mx, my)

    def handle_mouse_move(self, pos: Tuple[int, int]):
        mx, my = pos
        self.hover_idx = None
        if self.list_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    self.hover_idx = idx
                    break

        if self.is_panning:
            dx = mx - self.pan_start_mouse[0]
            dy = my - self.pan_start_mouse[1]
            if abs(dx) > 3 or abs(dy) > 3:
                self.pan_has_moved = True
            sx, sy = self.pan_start_world_px
            self.center_lat, self.center_lon = self.tile_mgr.world_px_to_latlon(sx - dx, sy - dy, self.zoom)

    def handle_mouse_down(self, pos: Tuple[int, int], button: int):
        if button != 1:
            return
        mx, my = pos

        # Buttons
        for bkey, r in self.btn_rects.items():
            if r.collidepoint(mx, my):
                if bkey in ("close", "cancel"):
                    if self.on_close_cb:
                        self.on_close_cb()
                elif bkey == "refresh":
                    self.refresh()
                elif bkey == "download_open":
                    self.download_selected()
                elif bkey == "upload_current":
                    if self.on_upload_current_cb:
                        self.on_upload_current_cb()
                    self.refresh()
                elif bkey == "toggle_layer":
                    self.layer = "osm" if self.layer == "sat" else "sat"
                elif bkey == "zoom_in":
                    self.zoom_at_point(self.map_rect.centerx, self.map_rect.centery, 1)
                elif bkey == "zoom_out":
                    self.zoom_at_point(self.map_rect.centerx, self.map_rect.centery, -1)
                elif bkey == "center_all":
                    gps_pts = [
                        (it["center_lat"], it["center_lon"])
                        for it in self.layouts
                        if it.get("center_lat") is not None and it.get("center_lon") is not None
                    ]
                    if gps_pts:
                        self.center_lat = sum(p[0] for p in gps_pts) / len(gps_pts)
                        self.center_lon = sum(p[1] for p in gps_pts) / len(gps_pts)
                return

        # List Cards
        if self.list_rect.collidepoint(mx, my):
            for idx, crect in enumerate(self.card_rects):
                if crect.collidepoint(mx, my):
                    now = time.time()
                    if self.selected_idx == idx and (now - self._last_click_ts) < 0.35:
                        self.download_selected()
                    else:
                        self.selected_idx = idx
                        self.center_on_layout(idx)
                    self._last_click_ts = now
                    return

        # Map Clicks
        if self.map_rect.collidepoint(mx, my):
            z = self.zoom
            cx_px, cy_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, z)
            mw_map, mh_map = self.map_rect.width, self.map_rect.height
            clicked_pin_idx = None
            for idx, it in enumerate(self.layouts):
                lat = it.get("center_lat")
                lon = it.get("center_lon")
                if lat is None or lon is None:
                    continue
                ex_px, ey_px = self.tile_mgr.latlon_to_world_px(lat, lon, z)
                px = int(mw_map / 2.0 + (ex_px - cx_px))
                py = int(mh_map / 2.0 + (ey_px - cy_px))
                if (mx - self.map_rect.x - px) ** 2 + (my - self.map_rect.y - py) ** 2 <= 144:
                    clicked_pin_idx = idx
                    break

            if clicked_pin_idx is not None:
                self.selected_idx = clicked_pin_idx
                self.center_on_layout(clicked_pin_idx)
                return

            self.is_panning = True
            self.pan_has_moved = False
            self.pan_start_mouse = (mx, my)
            self.pan_start_world_px = self.tile_mgr.latlon_to_world_px(self.center_lat, self.center_lon, self.zoom)
            return

        if not self.modal_rect.collidepoint(mx, my):
            if self.on_close_cb:
                self.on_close_cb()

    def handle_mouse_up(self, pos: Tuple[int, int], button: int):
        if button == 1:
            self.is_panning = False

    def handle_key_down(self, event: pygame.event.Event):
        if event.key == pygame.K_ESCAPE:
            if self.on_close_cb:
                self.on_close_cb()
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.download_selected()
        elif event.key == pygame.K_UP:
            if self.layouts:
                cur = self.selected_idx if self.selected_idx is not None else 0
                self.selected_idx = max(0, cur - 1)
                self.center_on_layout(self.selected_idx)
        elif event.key == pygame.K_DOWN:
            if self.layouts:
                cur = self.selected_idx if self.selected_idx is not None else 0
                self.selected_idx = min(len(self.layouts) - 1, cur + 1)
                self.center_on_layout(self.selected_idx)
        elif event.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            self.zoom = min(19, self.zoom + 1)
        elif event.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self.zoom = max(3, self.zoom - 1)
        elif event.key == pygame.K_r:
            self.refresh()
        elif event.key == pygame.K_u and not self.is_viewer_mode:
            if self.on_upload_current_cb:
                self.on_upload_current_cb()
            self.refresh()

    def handle_key(self, key: int, unicode: str = ""):
        """Compatibility wrapper for callers passing (key, unicode)."""
        class _KeyEvent:
            def __init__(self, k, u):
                self.key = k
                self.unicode = u
        self.handle_key_down(_KeyEvent(key, unicode))
