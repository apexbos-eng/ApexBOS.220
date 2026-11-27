# Copyright (c) 2026 AB
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# parse_packets.py

import json
import struct
import binascii
import os
import shutil
import textwrap
import re
from functools import lru_cache

_EVAL_GLOBALS = {"int": int}


@lru_cache(maxsize=512)
def _compile_eval_expr(expr: str):
    return compile(expr, "<rule_expr>", "eval")

class PacketValidationError(Exception):
    """Raised when a parsed value does not match the expected value in the rule."""
    pass

class PacketParser:
    def __init__(self, rules: list):
        self.rules = rules
        self.data = b''
        self.bit_pos = 0
        self.parsed_data = {}
        self.is_valid = True
        self.out_buffer = bytearray()
        self.out_bit_pos = 0
        self.context_stack = []
        self.flat_context = {}
        self.encode_context_stack = []
        self.encode_flat_context = {}

    def encode(self, data_dict: dict) -> bytes:
        """Generates a binary packet from a dictionary using the rules."""
        self.out_buffer = bytearray()
        self.out_bit_pos = 0
        self.encode_context_stack = [data_dict]
        self.encode_flat_context = {}
        for rule in self.rules:
            self._pack_rule(rule)
        return bytes(self.out_buffer)

    def parse(self, data: bytes) -> dict:
        self.data = data
        self.bit_pos = 0
        self.parsed_data = {}
        self.context_stack = [self.parsed_data]
        self.flat_context = {}
        self.is_valid = True
        
        try:
            for rule in self.rules:
                self._process_rule(rule)
        except EOFError:
            self.is_valid = False
        except PacketValidationError:
            self.is_valid = False
            
        return {
            "is_valid": self.is_valid,
            "data": self.parsed_data
        }

    def _normalize_rule(self, raw_rule: dict) -> dict:
        """Translates shorthand keys and type values to their standard long forms."""
        key_map = {
            't': 'type', 'n': 'name', 'e': 'expected', 'ei': 'expected_in',
            'f': 'format', 'w': 'lookup', 'c': 'condition', 'r': 'rules', 
            'l': 'length', 'i': 'item_type', 'x': 'terminator', 'ex': 'expression'
        }
        type_map = {
            'u8': 'uint8', 'i8': 'int8', 'u16': 'uint16', 'i16': 'int16', 
            'u32': 'uint32', 'i32': 'int32', 'u64': 'uint64', 'i64': 'int64', 
            'f16': 'float16', 'f32': 'float32', 'f64': 'float64', 'f': 'float',
            's': 'string', 's0': 'string_0', 'a': 'array', 'b': 'branch', 
            'k': 'skip', 'd': 'bcd', 'b1': 'bit', 'bl': 'bool', 'ib': 'int_bits',
            'cp': 'cpychk', 'sec': 'section', 'esec': 'end_section', 'prf': 'printf',
            'lp': 'loop', 'cl': 'calc'
        }
        
        rule = {}
        for k, v in raw_rule.items():
            norm_key = key_map.get(k, k)
            if norm_key in ['type', 'item_type'] and isinstance(v, str):
                rule[norm_key] = type_map.get(v, v)
            else:
                rule[norm_key] = v
        return rule

    def _get_basic_type_size_bits(self, type_str: str) -> int:
        format_map = {
            'uint8': 8, 'int8': 8,
            'uint16': 16, 'int16': 16,
            'uint32': 32, 'int32': 32,
            'uint64': 64, 'int64': 64,
            'float16': 16, 'float': 32,
            'float32': 32, 'float64': 64,
            'bit': 1, 'bool': 1
        }
        if type_str in format_map:
            return format_map[type_str]
        raise ValueError(f"Unknown or variable-length item type for array with 'end' length: {type_str}")

    def _process_rule(self, raw_rule: dict):
        self.flat_context["__cur_bit__"] = self.bit_pos
        self.flat_context["__cur_byte__"] = self.bit_pos // 8

        rule = self._normalize_rule(raw_rule)
        rule_type = rule.get("type")
        current_context = self.context_stack[-1]
        
        if rule_type == "section":
            name = rule.get("name")
            new_section = {}
            current_context[name] = new_section
            self.context_stack.append(new_section)
            return
            
        if rule_type == "end_section":
            if len(self.context_stack) > 1:
                self.context_stack.pop()
            return
            
        if rule_type == "branch":
            condition = rule.get("condition")
            if self._evaluate_condition(condition, self.flat_context):
                for sub_rule in rule.get("rules", []):
                    self._process_rule(sub_rule)
            return

        if rule_type == "loop":
            start_idx = rule.get("start", 0)
            stop_idx = rule.get("stop", 0)
            
            def resolve_idx(idx, context):
                if isinstance(idx, int): return idx
                if isinstance(idx, str) and idx in context: return int(context[idx])
                return 0

            start_val = resolve_idx(start_idx, self.flat_context)
            stop_val = resolve_idx(stop_idx, self.flat_context)
            
            loop_rules = rule.get("rules", [])
            
            def rename_recursive(r, suffix):
                mod_r = r.copy()
                if "name" in mod_r:
                    mod_r["name"] = f"{mod_r['name']}_{suffix}"
                if "rules" in mod_r:
                    mod_r["rules"] = [rename_recursive(sub, suffix) for sub in mod_r["rules"]]
                return mod_r
            
            for i in range(start_val, stop_val):
                for sub_rule in loop_rules:
                    mod_rule = rename_recursive(sub_rule, i)
                    self._process_rule(mod_rule)
            return

        if rule_type == "skip":
            if rule.get("length") == "end":
                remaining_bits = (len(self.data) * 8) - self.bit_pos
                self.bit_pos += remaining_bits
            else:
                length = self._resolve_length(rule, self.flat_context) if "length" in rule else 1
                if self.bit_pos + (length * 8) > len(self.data) * 8:
                    raise EOFError("Unexpected end of data while skipping.")
                self.bit_pos += length * 8
            return

        name = rule.get("name")
        if not name:
            raise ValueError("Rule missing 'name' attribute.")

        val = None

        if rule_type == "array":
            item_type = rule.get("item_type")
            if rule.get("length") == "end":
                item_size_bits = self._get_basic_type_size_bits(item_type)
                remaining_bits = (len(self.data) * 8) - self.bit_pos
                length = remaining_bits // item_size_bits
                val = [self._read_basic_type(item_type) for _ in range(length)]
            elif "length" in rule:
                length = self._resolve_length(rule, self.flat_context)
                val = [self._read_basic_type(item_type) for _ in range(length)]
            else:
                terminator = rule.get("terminator", 0)
                items = []
                while True:
                    v = self._read_basic_type(item_type)
                    if v == terminator:
                        break
                    items.append(v)
                val = items

        elif rule_type == "string":
            if rule.get("length") == "end":
                length = ((len(self.data) * 8) - self.bit_pos) // 8
            else:
                length = self._resolve_length(rule, self.flat_context)
            raw_bytes = self._read_bytes(length)
            val = raw_bytes.decode('utf-8', errors='replace').strip('\x00')

        elif rule_type == "string_0":
            chars = bytearray()
            while True:
                char_byte = self._read_bytes(1)
                if char_byte == b'\x00':
                    break
                chars.extend(char_byte)
            val = chars.decode('utf-8', errors='replace')

        elif rule_type == "bcd":
            if rule.get("length") == "end":
                length = ((len(self.data) * 8) - self.bit_pos) // 8
            else:
                length = self._resolve_length(rule, self.flat_context)
            raw_bytes = self._read_bytes(length)
            bcd_val = 0
            for b in raw_bytes:
                high = (b & 0xF0) >> 4
                low = b & 0x0F
                bcd_val = (bcd_val * 100) + (high * 10) + low
            val = bcd_val

        elif rule_type == "bit":
            val = self._read_bits(1)
            
        elif rule_type == "bool":
            val = bool(self._read_bits(1))
            
        elif rule_type == "int_bits":
            if rule.get("length") == "end":
                length = (len(self.data) * 8) - self.bit_pos
            else:
                length = self._resolve_length(rule, self.flat_context)
                if length < 0:
                    length = (len(self.data) * 8) - self.bit_pos + length
                    if length < 0:
                        length = 0
            
            val_int = self._read_bits(length)
            if "format" not in rule:
                val = f"{val_int:0{length}b}" if length > 0 else ""
            else:
                val = val_int

        elif rule_type == "cpychk":
            val = rule.get("length")

        elif rule_type == "printf":
            fmt_str = rule.get("format", "")
            try:
                val = fmt_str.format(**self.flat_context)
            except Exception:
                val = ""
            current_context[name] = val
            self.flat_context[name] = val
            return

        elif rule_type == "calc":
            expr = rule.get("expression", "")
            try:
                val = eval(_compile_eval_expr(expr), _EVAL_GLOBALS, self.flat_context)
            except Exception as ex:
                val = 0
                
            if "format" in rule:
                try:
                    val = format(val, rule["format"])
                except Exception:
                    pass
                    
            current_context[name] = val
            self.flat_context[name] = val
            return

        else:
            val = self._read_basic_type(rule_type)

        if "expected" in rule:
            expected_val = rule["expected"]
            if isinstance(expected_val, str) and expected_val.lower().startswith("0x"):
                try:
                    expected_val = int(expected_val, 16)
                except ValueError:
                    pass 
                    
            if val != expected_val:
                raise PacketValidationError(f"Field '{name}' expected {expected_val} but got {val}")

        if "expected_in" in rule:
            expected_in_list = rule["expected_in"]
            converted_list = []
            for item in expected_in_list:
                if isinstance(item, str) and item.lower().startswith("0x"):
                    try: 
                        converted_list.append(int(item, 16))
                    except ValueError: 
                        converted_list.append(item)
                else:
                    converted_list.append(item)
            
            if val not in converted_list:
                raise PacketValidationError(f"Field '{name}' expected one of {converted_list} but got {val}")

        if "lookup" in rule:
            lookup_dict = rule["lookup"]
            def apply_lookup(v):
                if v in lookup_dict:
                    return lookup_dict[v]
                if str(v) in lookup_dict:
                    return lookup_dict[str(v)]
                return v 
                
            if isinstance(val, list):
                val = [apply_lookup(v) for v in val]
            else:
                val = apply_lookup(val)

        if "format" in rule and rule_type not in ["printf", "calc"]:
            fmt = rule["format"]
            try:
                if isinstance(val, list):
                    val = [format(v, fmt) for v in val]
                else:
                    val = format(val, fmt)
            except Exception:
                pass

        current_context[name] = val
        self.flat_context[name] = val

    def _resolve_length(self, rule: dict, context: dict) -> int:
        length_val = rule.get("length")
        if isinstance(length_val, int):
            return length_val
        elif isinstance(length_val, str):
            if length_val in context:
                return int(context[length_val])
            else:
                raise ValueError(f"Dynamic length field '{length_val}' not found in context.")
        return 0

    def _evaluate_condition(self, condition_str: str, context: dict) -> bool:
        try:
            return eval(_compile_eval_expr(condition_str), _EVAL_GLOBALS, context)
        except Exception:
            return False

    def _read_bytes(self, length: int) -> bytes:
        if self.bit_pos % 8 == 0:
            byte_offset = self.bit_pos // 8
            if byte_offset + length > len(self.data):
                raise EOFError(f"Need {length} bytes, have {len(self.data) - byte_offset}")
            chunk = self.data[byte_offset:byte_offset + length]
            self.bit_pos += length * 8
            return chunk
        else:
            if self.bit_pos + (length * 8) > len(self.data) * 8:
                raise EOFError(f"Need {length} bytes, not enough bits left")
            out = bytearray(length)
            for i in range(length):
                out[i] = self._read_bits(8)
            return bytes(out)

    def _read_bits(self, num_bits: int) -> int:
        if self.bit_pos + num_bits > len(self.data) * 8:
            raise EOFError(f"Need {num_bits} bits, have {(len(self.data) * 8) - self.bit_pos}")
        
        value = 0
        for _ in range(num_bits):
            byte_index = self.bit_pos // 8
            bit_index = 7 - (self.bit_pos % 8) 
            bit = (self.data[byte_index] >> bit_index) & 1
            value = (value << 1) | bit
            self.bit_pos += 1
        return value

    def _read_basic_type(self, type_str: str):
        format_map = {
            'uint8': ('>B', 1),
            'int8': ('>b', 1),
            'uint16': ('>H', 2),
            'int16': ('>h', 2),
            'uint32': ('>I', 4),
            'int32': ('>i', 4),
            'uint64': ('>Q', 8),
            'int64': ('>q', 8),
            'float16': ('>e', 2),
            'float': ('>f', 4),
            'float32': ('>f', 4),
            'float64': ('>d', 8)
        }
        if type_str not in format_map:
            raise ValueError(f"Unknown type: {type_str}")
            
        fmt, size = format_map[type_str]
        chunk = self._read_bytes(size)
        return struct.unpack(fmt, chunk)[0]

    def _write_bits(self, value: int, num_bits: int):
        for i in range(num_bits - 1, -1, -1):
            bit = (value >> i) & 1
            byte_idx = self.out_bit_pos // 8
            bit_idx = 7 - (self.out_bit_pos % 8)
            
            if byte_idx == len(self.out_buffer):
                self.out_buffer.append(0)
                
            self.out_buffer[byte_idx] |= (bit << bit_idx)
            self.out_bit_pos += 1

    def _write_bytes(self, chunk: bytes):
        if self.out_bit_pos % 8 == 0:
            self.out_buffer.extend(chunk)
            self.out_bit_pos += len(chunk) * 8
        else:
            for b in chunk:
                self._write_bits(b, 8)

    def _pack_basic_type(self, type_str: str, value, rule: dict):
        format_map = {
            'uint8': ('>B', 1), 'int8': ('>b', 1),
            'uint16': ('>H', 2), 'int16': ('>h', 2),
            'uint32': ('>I', 4), 'int32': ('>i', 4),
            'uint64': ('>Q', 8), 'int64': ('>q', 8),
            'float16': ('>e', 2), 'float': ('>f', 4),
            'float32': ('>f', 4), 'float64': ('>d', 8)
        }
        if type_str not in format_map:
            raise ValueError(f"Unknown type: {type_str}")
            
        fmt, size = format_map[type_str]
        
        if 'f' in fmt or 'e' in fmt or 'd' in fmt:
            try: value = float(value)
            except: value = 0.0
        else:
            if isinstance(value, str):
                try: value = int(value, 0)
                except: value = 0
            else:
                try: value = int(value)
                except: value = 0
                
        try:
            chunk = struct.pack(fmt, value)
            self._write_bytes(chunk)
        except struct.error:
            self._write_bytes(b'\x00' * size)

    def _pack_rule(self, raw_rule: dict):
        self.encode_flat_context["__cur_bit__"] = self.out_bit_pos
        self.encode_flat_context["__cur_byte__"] = self.out_bit_pos // 8

        rule = self._normalize_rule(raw_rule)
        rule_type = rule.get("type")
        current_context = self.encode_context_stack[-1]
        
        if rule_type == "section":
            name = rule.get("name")
            new_section = current_context.get(name, {})
            self.encode_context_stack.append(new_section)
            return
            
        if rule_type == "end_section":
            if len(self.encode_context_stack) > 1:
                self.encode_context_stack.pop()
            return
        
        if rule_type == "branch":
            condition = rule.get("condition")
            if self._evaluate_condition(condition, self.encode_flat_context):
                for sub_rule in rule.get("rules", []):
                    self._pack_rule(sub_rule)
            return

        if rule_type == "loop":
            start_idx = rule.get("start", 0)
            stop_idx = rule.get("stop", 0)
            
            def resolve_idx(idx, context):
                if isinstance(idx, int): return idx
                if isinstance(idx, str) and idx in context: return int(context[idx])
                return 0

            start_val = resolve_idx(start_idx, self.encode_flat_context)
            stop_val = resolve_idx(stop_idx, self.encode_flat_context)
            
            loop_rules = rule.get("rules", [])
            
            def rename_recursive(r, suffix):
                mod_r = r.copy()
                if "name" in mod_r:
                    mod_r["name"] = f"{mod_r['name']}_{suffix}"
                if "rules" in mod_r:
                    mod_r["rules"] = [rename_recursive(sub, suffix) for sub in mod_r["rules"]]
                return mod_r

            for i in range(start_val, stop_val):
                for sub_rule in loop_rules:
                    mod_rule = rename_recursive(sub_rule, i)
                    self._pack_rule(mod_rule)
            return

        if rule_type == "skip":
            if rule.get("length") == "end":
                length = 0
            else:
                length = self._resolve_length(rule, self.encode_flat_context) if "length" in rule else 1
            self._write_bytes(b'\x00' * length)
            return

        name = rule.get("name")
        if not name:
            return

        if rule_type == "printf":
            fmt_str = rule.get("format", "")
            try:
                val = fmt_str.format(**self.encode_flat_context)
            except:
                val = ""
            self.encode_flat_context[name] = val
            return

        elif rule_type == "calc":
            expr = rule.get("expression", "")
            try:
                val = eval(_compile_eval_expr(expr), _EVAL_GLOBALS, self.encode_flat_context)
            except Exception:
                val = 0
                
            if "format" in rule:
                try:
                    val = format(val, rule["format"])
                except Exception:
                    pass
                    
            self.encode_flat_context[name] = val
            return

        val = current_context.get(name)
        if val is None:
            if "expected" in rule:
                val = rule["expected"]
                if isinstance(val, str) and val.lower().startswith("0x"):
                    val = int(val, 16)
            elif "expected_in" in rule and isinstance(rule["expected_in"], list) and len(rule["expected_in"]) > 0:
                val = rule["expected_in"][0]
                if isinstance(val, str) and val.lower().startswith("0x"):
                    val = int(val, 16)
            else:
                val = [] if rule_type == "array" else 0

        self.encode_flat_context[name] = val

        if "lookup" in rule:
            lookup_dict = rule["lookup"]
            rev_lookup = {str(v): k for k, v in lookup_dict.items()}
            def unlookup(v):
                sv = str(v)
                if sv in rev_lookup:
                    k = rev_lookup[sv]
                    return int(k) if str(k).isdigit() else k
                return v
            if isinstance(val, list):
                val = [unlookup(v) for v in val]
            else:
                val = unlookup(val)

        fmt_hint = rule.get("format", "")
        def unformat(v):
            if isinstance(v, str):
                if 'b' in fmt_hint or 'B' in fmt_hint:
                    try: return int(v, 2)
                    except: pass
                elif 'x' in fmt_hint or 'X' in fmt_hint:
                    try: return int(v, 16)
                    except: pass
            return v

        if isinstance(val, list):
            val = [unformat(v) for v in val]
        else:
            val = unformat(val)

        if rule_type == "array":
            item_type = rule.get("item_type")
            if not isinstance(val, list): val = []
            
            for item in val:
                self._pack_basic_type(item_type, item, rule)
            if "length" not in rule and "terminator" in rule:
                self._pack_basic_type(item_type, rule["terminator"], rule)

        elif rule_type == "string":
            if isinstance(val, str): val = val.encode('utf-8')
            elif isinstance(val, int): val = str(val).encode('utf-8')
            else: val = bytes(val) if isinstance(val, (bytes, bytearray)) else b''
            
            if rule.get("length") == "end":
                length = len(val)
            else:
                length = self._resolve_length(rule, self.encode_flat_context)
            
            if len(val) < length:
                val = val + b'\x00' * (length - len(val))
            else:
                val = val[:length]
            self._write_bytes(val)

        elif rule_type == "string_0":
            if isinstance(val, str): val = val.encode('utf-8')
            elif isinstance(val, int): val = str(val).encode('utf-8')
            self._write_bytes(val + b'\x00')

        elif rule_type == "bcd":
            try: val_int = int(val)
            except: val_int = 0
            
            if rule.get("length") == "end":
                length = max(1, (len(str(val_int)) + 1) // 2)
            else:
                length = self._resolve_length(rule, self.encode_flat_context)
            
            bcd_bytes = bytearray(length)
            for i in range(length - 1, -1, -1):
                low = val_int % 10
                val_int //= 10
                high = val_int % 10
                val_int //= 10
                bcd_bytes[i] = (high << 4) | low
            self._write_bytes(bytes(bcd_bytes))

        elif rule_type == "bit":
            self._write_bits(1 if val else 0, 1)

        elif rule_type == "bool":
            self._write_bits(1 if val else 0, 1)

        elif rule_type == "int_bits":
            is_bin_str = isinstance(val, str) and all(c in '01' for c in val) if isinstance(val, str) else False
            
            if rule.get("length") == "end":
                if is_bin_str:
                    try: val_int = int(val, 2)
                    except: val_int = 0
                    length = len(val)
                else:
                    try: val_int = int(val)
                    except: val_int = 0
                    needed_bits = max(1, val_int.bit_length())
                    remainder = (self.out_bit_pos + needed_bits) % 8
                    length = needed_bits + (8 - remainder) if remainder != 0 else needed_bits
            else:
                length = self._resolve_length(rule, self.encode_flat_context)
                if length < 0:
                    if is_bin_str:
                        try: val_int = int(val, 2)
                        except: val_int = 0
                        length = len(val)
                    else:
                        try: val_int = int(val)
                        except: val_int = 0
                        length = max(1, val_int.bit_length())
                else:
                    if is_bin_str:
                        try: val_int = int(val, 2)
                        except: val_int = 0
                    else:
                        try: val_int = int(val)
                        except: val_int = 0
                        
            self._write_bits(val_int, length)

        elif rule_type == "cpychk":
            pass

        else:
            self._pack_basic_type(rule_type, val, rule)

    def _generate_box_lines(self, width=None):
        RESET = "\033[0m"
        BOLD = "\033[1m"
        CYAN = "\033[96m"
        GREEN = "\033[92m"
        RED = "\033[91m"
        YELLOW = "\033[93m"
        MAGENTA = "\033[95m"

        title_color = GREEN if self.is_valid else RED
        status = "VALID" if self.is_valid else "INVALID / TRUNCATED"
        
        if width is None:
            term_width = shutil.get_terminal_size(fallback=(80, 20)).columns
        else:
            term_width = width
            
        box_width = max(50, term_width - 4) 
        
        lines = []

        def format_node(node, depth=0):
            indent = " " * (depth * 2)
            for key, value in node.items():
                if isinstance(value, dict):
                    lines.append("")
                    sec_title = f" {key} "
                    dash_count = box_width - len(sec_title) - len(indent)
                    if dash_count < 0: dash_count = 0
                    left_dashes = dash_count // 2
                    right_dashes = dash_count - left_dashes
                    lines.append(f"{indent}{CYAN}" + "─" * left_dashes + sec_title + "─" * right_dashes + f"{RESET}")
                    
                    format_node(value, depth + 1)
                    lines.append("")
                elif isinstance(value, list):
                    lines.append(f"{indent}{MAGENTA}{key}:{RESET}")
                    arr_str = ", ".join(repr(x) if isinstance(x, str) else str(x) for x in value)
                    arr_str = f"[{arr_str}]"
                    wrap_width = box_width - len(indent) - 4
                    wrapped = textwrap.wrap(arr_str, width=wrap_width if wrap_width > 10 else 10)
                    for w in wrapped:
                        lines.append(f"{indent}  {YELLOW}{w}{RESET}")
                else:
                    lines.append(f"{indent}{MAGENTA}{key}:{RESET} {YELLOW}{value}{RESET}")

        format_node(self.parsed_data)
        
        box_lines = []
        box_lines.append(f"{CYAN}╔" + "═" * box_width + f"╗{RESET}")
        header = f" Packet Parse Result [Status: {status}] "
        box_lines.append(f"{CYAN}║{RESET}{BOLD}{title_color}{header.center(box_width)}{RESET}{CYAN}║{RESET}")
        box_lines.append(f"{CYAN}╠" + "═" * box_width + f"╣{RESET}")
        
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        
        for line in lines:
            visible_len = len(ansi_escape.sub('', line))
            
            if visible_len <= box_width:
                padded_line = line + " " * (box_width - visible_len)
            else:
                padded_line = line
                
            box_lines.append(f"{CYAN}║{RESET}{padded_line}{CYAN}║{RESET}")

        box_lines.append(f"{CYAN}╚" + "═" * box_width + f"╝{RESET}")
        return box_lines

    def get_formatted_string(self, x=0, y=0, width=None, clear_screen=False) -> str:
        box_lines = self._generate_box_lines(width)
        output = []
        
        if clear_screen:
            output.append("\033[2J") 
            
        current_y = y + 1 
        for line in box_lines:
            output.append(f"\033[{current_y};{x + 1}H{line}")
            current_y += 1
            
        output.append("\033[0m")
        return "".join(output)

    def display_formatted(self):
        box_lines = self._generate_box_lines()
        for line in box_lines:
            print(line)


def parse_packet_from_file(data, rule_file_path: str, print_output: bool = False) -> str:
    if isinstance(data, str):
        packet_bytes = binascii.unhexlify(data.replace(" ", ""))
    elif isinstance(data, (bytes, bytearray)):
        packet_bytes = data
    else:
        raise TypeError("Data must be a hex string or bytes/bytearray")

    with open(rule_file_path, 'r') as f:
        rules_doc = json.load(f)
        
    parser = PacketParser(rules_doc.get("rules", []))
    result = parser.parse(packet_bytes)
    
    if print_output:
        parser.display_formatted()
        
    return json.dumps(result, indent=4)


if __name__ == "__main__":
    test_results = []
    
    def run_test(test_name, data, rule_file, expect_valid=True):
        print(f"\n--- {test_name} ---")
        result_json = parse_packet_from_file(data, rule_file, print_output=True)
        result_dict = json.loads(result_json)
        passed = (result_dict.get("is_valid") == expect_valid)
        test_results.append((test_name, passed))
        if passed:
            print(f"\033[92m[✓] Test Passed\033[0m (Expected valid={expect_valid})")
        else:
            print(f"\033[91m[x] Test Failed\033[0m (Expected valid={expect_valid}, got {result_dict.get('is_valid')})")

    # --- Test Setup ---
    sample_rules = {
        "rules": [
            {"type": "uint8", "name": "header_version"},
            {"type": "uint8", "name": "msg_type"},
            {"type": "skip", "length": 2}, 
            {"type": "string", "name": "system_id", "length": 4},
            {"type": "branch", "condition": "msg_type == 10", "rules": [
                {"type": "uint16", "name": "speed"},
                {"type": "array", "name": "sensor_readings", "item_type": "uint8", "length": 3}
            ]},
            {"type": "branch", "condition": "msg_type == 20", "rules": [
                {"type": "float", "name": "temperature"}
            ]}
        ]
    }
    
    rule_filename = "test_rules.json"
    with open(rule_filename, "w") as f:
        json.dump(sample_rules, f, indent=4)

    # --- Test 1: Branch condition A (msg_type == 10), passing as Bytes ---
    packet_1_bytes = struct.pack(">BB 2s 4s H BBB", 
                                 1, 10, b'\x00\x00', b'PTC1', 1050, 10, 20, 30)
    run_test("Test 1: Full Valid Bytes Input", packet_1_bytes, rule_filename)

    # --- Test 2: Incomplete/Truncated Packet ---
    packet_truncated = packet_1_bytes[:-4]
    run_test("Test 2: Truncated Bytes Input (Should fail gracefully)", packet_truncated, rule_filename, expect_valid=False)

    # --- Test 3: Large Hex String test ---
    hex_input = "1854881EA40F5500571854881EA40F00555E10004218000014FAA7C0772E626E73662E3433313031352E32302E77646300622E626E73662E6374632E3433313300041BD1001100000000001B1F6AE3EA9E29005F05ADD8000000000000"
    run_test("Test 3: Target Hex String Input", hex_input, rule_filename)

    # --- Test 4: Dynamic Lengths and Null-terminated strings ---
    dyn_rules = {
        "rules": [
            {"type": "uint8", "name": "str_len"},
            {"type": "string", "name": "dyn_str", "length": "str_len"},
            {"type": "string_0", "name": "null_str"},
            {"type": "uint8", "name": "arr_len"},
            {"type": "array", "name": "dyn_arr", "item_type": "uint8", "length": "arr_len"},
            {"type": "array", "name": "term_arr", "item_type": "uint8"},
            {"type": "bcd", "name": "device_bcd_id", "length": 2}
        ]
    }
    
    dyn_rule_filename = "test_dyn_rules.json"
    with open(dyn_rule_filename, "w") as f:
        json.dump(dyn_rules, f, indent=4)

    packet_4_bytes = struct.pack(">B 3s 3s B BB BBB BB", 3, b'ABC', b'Hi\x00', 2, 10, 20, 99, 88, 0, 0x45, 0x67)
    run_test("Test 4: Dynamic Lengths, string_0, and BCD Input", packet_4_bytes, dyn_rule_filename)

    # --- Test 5: Bits, Booleans, arbitrary length ints, and 64-bit types ---
    bit_rules = {
        "rules": [
            {"type": "bit", "name": "flag_1"},
            {"type": "bool", "name": "is_active"},
            {"type": "int_bits", "name": "custom_6bit_int", "length": 6},
            {"type": "uint64", "name": "large_id"},
            {"type": "float64", "name": "precise_temp"}
        ]
    }
    
    bit_rule_filename = "test_bit_rules.json"
    with open(bit_rule_filename, "w") as f:
        json.dump(bit_rules, f, indent=4)

    packet_5_bytes = struct.pack(">B Q d", 0xEA, 0x0102030405060708, 3.1415926535)
    run_test("Test 5: Bits, Bools, arbitrary int_bits, and 64-bit types", packet_5_bytes, bit_rule_filename)

    # --- Test 6: Comprehensive test of ALL supported types and functionalities ---
    all_features_rules = {
        "rules": [
            {"type": "uint8", "name": "test_uint8"},
            {"type": "int8", "name": "test_int8"},
            {"type": "uint16", "name": "test_uint16"},
            {"type": "int16", "name": "test_int16"},
            {"type": "uint32", "name": "test_uint32"},
            {"type": "int32", "name": "test_int32"},
            {"type": "uint64", "name": "test_uint64"},
            {"type": "int64", "name": "test_int64"},
            {"type": "float16", "name": "test_float16"},
            {"type": "float32", "name": "test_float32"},
            {"type": "float64", "name": "test_float64"},
            {"type": "bit", "name": "test_bit"},
            {"type": "bool", "name": "test_bool"},
            {"type": "int_bits", "name": "test_int_bits", "length": 6},
            {"type": "skip", "length": 2},
            {"type": "uint8", "name": "test_str_len"},
            {"type": "string", "name": "test_dyn_str", "length": "test_str_len"},
            {"type": "string_0", "name": "test_string_0"},
            {"type": "uint8", "name": "test_arr_len"},
            {"type": "array", "name": "test_dyn_array", "item_type": "int8", "length": "test_arr_len"},
            {"type": "array", "name": "test_term_array", "item_type": "uint16", "terminator": 65535},
            {"type": "bcd", "name": "test_bcd", "length": 2},
            {"type": "cpychk", "name": "test_cpychk", "length": 999},
            {"type": "branch", "condition": "test_uint8 == 1 and test_bool == False", "rules": [
                {"type": "string", "name": "branch_hit", "length": 7}
            ]}
        ]
    }
    
    all_features_filename = "test_all_features.json"
    with open(all_features_filename, "w") as f:
        json.dump(all_features_rules, f, indent=4)

    packet_6_bytes = struct.pack(
        ">B b H h I i Q q e f d B 2s B 4s 6s B b b b H H H 2s 7s",
        1, -2, 300, -300, 400000, -400000, 5000000000, -5000000000, 
        3.14, 2.718, 1.61803, 
        0xAA, 
        b'\x00\x00', 
        4, 
        b'TEST', 
        b'Hello\x00', 
        3, 
        10, -20, 30, 
        100, 200, 0xFFFF, 
        b'\x12\x34', 
        b'BRANCH!'
    )
    run_test("Test 6: Comprehensive All-Features Test", packet_6_bytes, all_features_filename)

    # --- Test 7: Expected Value Validation ---
    validation_rules = {
        "rules": [
            {"type": "uint8", "name": "magic_byte", "expected": "0xAA"},
            {"type": "uint8", "name": "version", "expected": 1},
            {"type": "branch", "condition": "version == 0x01", "rules": [
                {"type": "string", "name": "payload", "length": 4}
            ]}
        ]
    }
    validation_filename = "test_validation.json"
    with open(validation_filename, "w") as f:
        json.dump(validation_rules, f, indent=4)

    packet_7_valid = struct.pack(">B B 4s", 170, 1, b'DATA')
    run_test("Test 7a: Expected Value Validation (Valid)", packet_7_valid, validation_filename)

    packet_7_invalid = struct.pack(">B B 4s", 170, 2, b'DATA')
    run_test("Test 7b: Expected Value Validation (Invalid Version)", packet_7_invalid, validation_filename, expect_valid=False)

    # --- Test 8: Output Formatting ---
    format_rules = {
        "rules": [
            {"type": "uint8", "name": "hex_byte", "format": "02X"},
            {"type": "float32", "name": "temperature", "format": ".2f"},
            {"type": "uint16", "name": "binary_flags", "format": "016b"},
            {"type": "array", "name": "mac_address", "item_type": "uint8", "length": 6, "format": "02x"}
        ]
    }
    format_filename = "test_format.json"
    with open(format_filename, "w") as f:
        json.dump(format_rules, f, indent=4)

    packet_8_bytes = struct.pack(">B f H 6B", 255, 98.61234, 43690, 0x00, 0x1A, 0x2B, 0x3C, 0x4D, 0x5E)
    run_test("Test 8: Output Formatting", packet_8_bytes, format_filename)

    # --- Test 9: Lookup Dictionary ---
    lookup_rules = {
        "rules": [
            {"type": "uint8", "name": "msg_type", "lookup": {
                0: "N/A",
                1: "Short Broadcast",
                2: "Long Broadcast",
                3: "Unicast"
            }},
            {"type": "array", "name": "flags", "item_type": "uint8", "length": 3, "lookup": {
                10: "Active",
                20: "Inactive",
                30: "Pending"
            }}
        ]
    }
    lookup_filename = "test_lookup.json"
    with open(lookup_filename, "w") as f:
        json.dump(lookup_rules, f, indent=4)

    packet_9_bytes = struct.pack(">B 3B", 2, 10, 30, 99)
    run_test("Test 9: Lookup Dictionary", packet_9_bytes, lookup_filename)

    # --- Test 10: Shorthand Syntax ---
    shorthand_rules = {
        "rules": [
            {"t": "u8", "n": "version", "e": "0x01"},
            {"t": "k", "l": 1},
            {"t": "s", "n": "id", "l": 4},
            {"t": "a", "n": "data", "i": "u16", "l": 2, "f": "04X", "w": {43690: "AAAA", 48059: "BBBB"}}
        ]
    }
    shorthand_filename = "test_shorthand.json"
    with open(shorthand_filename, "w") as f:
        json.dump(shorthand_rules, f, indent=4)

    packet_10_bytes = struct.pack(">B B 4s H H", 1, 0xFF, b'WXYZ', 43690, 48059)
    run_test("Test 10: Shorthand Syntax Test", packet_10_bytes, shorthand_filename)

    # --- Test 11: Packet Generation (JSON to binary) ---
    print("\n--- Test 11: Packet Generation ---")
    gen_rules = {
        "rules": [
            {"type": "uint8", "name": "version"},
            {"type": "bit", "name": "flag"},
            {"type": "int_bits", "name": "custom_val", "length": 7},
            {"type": "string", "name": "id", "length": 4},
            {"type": "array", "name": "data", "item_type": "uint16", "length": 2, "format": "04X"}
        ]
    }
    gen_filename = "test_gen.json"
    with open(gen_filename, "w") as f:
        json.dump(gen_rules, f, indent=4)
        
    parser_gen = PacketParser(gen_rules["rules"])
    
    data_to_encode = {
        "version": 2,
        "flag": 1,
        "custom_val": "0101010",
        "id": "NODE",
        "data": ["0A0B", "0C0D"]
    }
    
    encoded_bytes = parser_gen.encode(data_to_encode)
    encoded_hex = binascii.hexlify(encoded_bytes).decode('utf-8').upper()
    print(f"Generated Hex: {encoded_hex}")
    
    result_11 = parser_gen.parse(encoded_bytes)
    passed_11 = result_11["is_valid"] and result_11["data"] == data_to_encode
    test_results.append(("Test 11: Packet Generation", passed_11))
    
    if passed_11:
         print(f"\033[92m[✓] Test Passed\033[0m (Generated bytes match parsed)")
    else:
         print(f"\033[91m[x] Test Failed\033[0m")
         print("Expected:", data_to_encode)
         print("Got:", result_11["data"])

    # --- Test 12: Copy Check (cpychk) Type ---
    print("\n--- Test 12: Copy Check (cpychk) Type ---")
    cpychk_rules = {
        "rules": [
            {"type": "uint8", "name": "version"},
            {"type": "cpychk", "name": "copied_flag", "length": 12345}
        ]
    }
    cpychk_filename = "test_cpychk.json"
    with open(cpychk_filename, "w") as f:
        json.dump(cpychk_rules, f, indent=4)
        
    packet_12_bytes = struct.pack(">B", 1)
    
    result_12_json = parse_packet_from_file(packet_12_bytes, cpychk_filename, print_output=True)
    result_12_dict = json.loads(result_12_json)
    
    passed_12 = result_12_dict.get("is_valid") and result_12_dict["data"].get("copied_flag") == 12345
    test_results.append(("Test 12: Copy Check (cpychk)", passed_12))
    
    if passed_12:
        print(f"\033[92m[✓] Test Passed\033[0m (cpychk assigned 12345)")
    else:
        print(f"\033[91m[x] Test Failed\033[0m (Expected copied_flag=12345)")

    # --- Test 13: Sections and Printf ---
    print("\n--- Test 13: Sections and Printf ---")
    section_rules = {
        "rules": [
            {"type": "uint8", "name": "header_id"},
            {"type": "section", "name": "sensor_data"},
                {"type": "uint16", "name": "temp_raw"},
                {"type": "uint8", "name": "status"},
                {"type": "printf", "name": "summary", "format": "Sensor {header_id}: temp={temp_raw}, status={status}"},
                {"type": "section", "name": "metadata"},
                    {"type": "uint8", "name": "checksum"},
                {"type": "end_section"},
            {"type": "end_section"},
            {"type": "uint8", "name": "footer_id"}
        ]
    }
    section_filename = "test_sections.json"
    with open(section_filename, "w") as f:
        json.dump(section_rules, f, indent=4)
        
    packet_13_bytes = struct.pack(">B H B B B", 55, 1024, 1, 99, 56)
    
    result_13_json = parse_packet_from_file(packet_13_bytes, section_filename, print_output=True)
    result_13_dict = json.loads(result_13_json)
    
    expected_data = {
        "header_id": 55,
        "sensor_data": {
            "temp_raw": 1024,
            "status": 1,
            "summary": "Sensor 55: temp=1024, status=1",
            "metadata": {
                "checksum": 99
            }
        },
        "footer_id": 56
    }
    
    passed_13 = result_13_dict.get("is_valid") and result_13_dict.get("data") == expected_data
    test_results.append(("Test 13: Sections and Printf", passed_13))
    
    if passed_13:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")
        
    # --- Test 14: Expected In Validation ---
    print("\n--- Test 14: Expected In Validation ---")
    expected_in_rules = {
        "rules": [
            {"type": "uint8", "name": "magic", "expected_in": ["0xAA", "0xBB"]},
            {"type": "uint8", "name": "version", "expected_in": [1, 2, 3]}
        ]
    }
    expected_in_filename = "test_expected_in.json"
    with open(expected_in_filename, "w") as f:
        json.dump(expected_in_rules, f, indent=4)

    packet_14_valid = struct.pack(">B B", 170, 2)
    run_test("Test 14a: Expected In Validation (Valid)", packet_14_valid, expected_in_filename)

    packet_14_invalid = struct.pack(">B B", 170, 5) 
    run_test("Test 14b: Expected In Validation (Invalid)", packet_14_invalid, expected_in_filename, expect_valid=False)

    # --- Test 15: Loop Type ---
    print("\n--- Test 15: Loop Type ---")
    loop_rules = {
        "rules": [
            {"type": "uint8", "name": "count"},
            {"type": "loop", "start": 0, "stop": "count", "rules": [
                {"type": "uint16", "name": "sensor_val"}
            ]}
        ]
    }
    loop_filename = "test_loop.json"
    with open(loop_filename, "w") as f:
        json.dump(loop_rules, f, indent=4)
        
    packet_15_bytes = struct.pack(">B H H H", 3, 100, 200, 300)
    
    result_15_json = parse_packet_from_file(packet_15_bytes, loop_filename, print_output=True)
    result_15_dict = json.loads(result_15_json)
    
    passed_15 = (result_15_dict.get("is_valid") 
                 and result_15_dict["data"].get("sensor_val_0") == 100 
                 and result_15_dict["data"].get("sensor_val_1") == 200 
                 and result_15_dict["data"].get("sensor_val_2") == 300)
    test_results.append(("Test 15: Loop Type Execution", passed_15))
    
    if passed_15:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 16: calc type and int_bits negative length ---
    print("\n--- Test 16: calc type and int_bits negative length ---")
    calc_rules = {
        "rules": [
            {"type": "uint8", "name": "val1"},
            {"type": "uint8", "name": "val2"},
            {"type": "calc", "name": "sum_val", "expression": "val1 + val2"},
            {"type": "calc", "name": "complex_calc", "expression": "int((val1 * 10) / 3) << 2"},
            {"type": "int_bits", "name": "remainder", "length": -4},
            {"type": "int_bits", "name": "last_nibble", "length": 4}
        ]
    }
    calc_filename = "test_calc.json"
    with open(calc_filename, "w") as f:
        json.dump(calc_rules, f, indent=4)
        
    packet_16_bytes = struct.pack(">B B H", 10, 20, 0xABCD)
    result_16_json = parse_packet_from_file(packet_16_bytes, calc_filename, print_output=True)
    result_16_dict = json.loads(result_16_json)
    
    passed_16 = (result_16_dict.get("is_valid")
                 and result_16_dict["data"].get("sum_val") == 30
                 and result_16_dict["data"].get("complex_calc") == (int((10 * 10) / 3) << 2)
                 and result_16_dict["data"].get("remainder") == "101010111100"
                 and result_16_dict["data"].get("last_nibble") == "1101")
    test_results.append(("Test 16: calc type and int_bits negative length", passed_16))
    
    if passed_16:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 17: length 'end' and context keywords ---
    print("\n--- Test 17: length 'end' and context keywords ---")
    end_rules = {
        "rules": [
            {"type": "uint8", "name": "val1"},
            {"type": "calc", "name": "bit_offset_start", "expression": "__cur_bit__"},
            {"type": "calc", "name": "byte_offset_start", "expression": "__cur_byte__"},
            {"type": "string", "name": "rest_of_str", "length": "end"}
        ]
    }
    end_filename = "test_end.json"
    with open(end_filename, "w") as f:
        json.dump(end_rules, f, indent=4)
        
    packet_17_bytes = struct.pack(">B 10s", 5, b'HelloWorld')
    result_17_json = parse_packet_from_file(packet_17_bytes, end_filename, print_output=True)
    result_17_dict = json.loads(result_17_json)
    
    passed_17 = (result_17_dict.get("is_valid")
                 and result_17_dict["data"].get("val1") == 5
                 and result_17_dict["data"].get("bit_offset_start") == 8
                 and result_17_dict["data"].get("byte_offset_start") == 1
                 and result_17_dict["data"].get("rest_of_str") == "HelloWorld")
    test_results.append(("Test 17: length 'end' and pointer keywords", passed_17))
    
    if passed_17:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 18: Unaligned Byte Boundaries ---
    print("\n--- Test 18: Unaligned Byte Boundaries ---")
    unaligned_rules = {
        "rules": [
            {"type": "int_bits", "name": "nibble1", "length": 4},
            {"type": "uint8", "name": "shifted_byte"},
            {"type": "int_bits", "name": "nibble2", "length": 4}
        ]
    }
    unaligned_filename = "test_unaligned.json"
    with open(unaligned_filename, "w") as f:
        json.dump(unaligned_rules, f, indent=4)
        
    packet_18_bytes = struct.pack(">BB", 0xAB, 0xCD)
    result_18_json = parse_packet_from_file(packet_18_bytes, unaligned_filename, print_output=True)
    result_18_dict = json.loads(result_18_json)
    
    passed_18 = (result_18_dict.get("is_valid")
                 and result_18_dict["data"].get("nibble1") == "1010"
                 and result_18_dict["data"].get("shifted_byte") == 0xBC
                 and result_18_dict["data"].get("nibble2") == "1101")
    test_results.append(("Test 18: Unaligned Byte Boundaries Parsing", passed_18))
    
    if passed_18:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 19: Unaligned Byte Boundaries Encoding ---
    print("\n--- Test 19: Unaligned Byte Boundaries Encoding ---")
    parser_unaligned = PacketParser(unaligned_rules["rules"])
    encoded_19 = parser_unaligned.encode({
        "nibble1": "1010",
        "shifted_byte": 0xBC,
        "nibble2": "1101"
    })
    
    passed_19 = (encoded_19 == packet_18_bytes)
    test_results.append(("Test 19: Unaligned Byte Boundaries Encoding", passed_19))
    
    if passed_19:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 20: Dynamic end length leading bits shifting ---
    print("\n--- Test 20: Dynamic end length leading bits shifting ---")
    end_shift_rules_14 = {
        "rules": [
            {"type": "int_bits", "name": "part1", "length": 40},
            {"type": "int_bits", "name": "part2", "length": 14},
            {"type": "int_bits", "name": "part3", "length": "end"}
        ]
    }
    end_shift_rules_15 = {
        "rules": [
            {"type": "int_bits", "name": "part1", "length": 40},
            {"type": "int_bits", "name": "part2", "length": 15},
            {"type": "int_bits", "name": "part3", "length": "end"}
        ]
    }
    with open("test_end_shift_14.json", "w") as f: json.dump(end_shift_rules_14, f)
    with open("test_end_shift_15.json", "w") as f: json.dump(end_shift_rules_15, f)
    
    packet_20 = struct.pack(">Q", 0xFFFFFFFFFF000155)
    
    res_14 = json.loads(parse_packet_from_file(packet_20, "test_end_shift_14.json"))
    res_15 = json.loads(parse_packet_from_file(packet_20, "test_end_shift_15.json"))
    
    passed_20 = (res_14["data"]["part3"] != res_15["data"]["part3"]) and (len(res_14["data"]["part3"]) == 10) and (len(res_15["data"]["part3"]) == 9)
    test_results.append(("Test 20: int_bits 'end' string length shifts", passed_20))

    if passed_20:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Test 21: calc type with format ---
    print("\n--- Test 21: calc type with format ---")
    calc_fmt_rules = {
        "rules": [
            {"type": "uint8", "name": "base_val"},
            {"type": "calc", "name": "hex_calc", "expression": "base_val * 2", "format": "04X"},
            {"type": "calc", "name": "float_calc", "expression": "base_val / 3", "format": ".2f"}
        ]
    }
    calc_fmt_filename = "test_calc_fmt.json"
    with open(calc_fmt_filename, "w") as f:
        json.dump(calc_fmt_rules, f, indent=4)
        
    packet_21_bytes = struct.pack(">B", 50) 
    result_21_json = parse_packet_from_file(packet_21_bytes, calc_fmt_filename, print_output=True)
    result_21_dict = json.loads(result_21_json)
    
    passed_21 = (result_21_dict.get("is_valid")
                 and result_21_dict["data"].get("hex_calc") == "0064"
                 and result_21_dict["data"].get("float_calc") == "16.67")
    test_results.append(("Test 21: calc type with format", passed_21))
    
    if passed_21:
        print(f"\033[92m[✓] Test Passed\033[0m")
    else:
        print(f"\033[91m[x] Test Failed\033[0m")

    # --- Print Summary Report ---
    print("\n")
    print("\033[1m\033[96m" + "═" * 70 + "\033[0m")
    print("\033[1m\033[96m║\033[0m" + "TEST SUMMARY REPORT".center(68) + "\033[1m\033[96m║\033[0m")
    print("\033[1m\033[96m╠" + "═" * 68 + "╣\033[0m")
    
    passed_count = 0
    for name, passed in test_results:
        if passed:
            status_str = "\033[92m[PASS]\033[0m"
            passed_count += 1
        else:
            status_str = "\033[91m[FAIL]\033[0m"
            
        line = f" {status_str} {name}"
        visible_len = 7 + len(name) 
        padding = " " * max(0, 68 - visible_len)
        print(f"\033[1m\033[96m║\033[0m{line}{padding}\033[1m\033[96m║\033[0m")

    print("\033[1m\033[96m╠" + "═" * 68 + "╣\033[0m")
    total_tests = len(test_results)
    failed_count = total_tests - passed_count
    
    summary_line = f" TOTAL: {total_tests} | PASSED: {passed_count} | FAILED: {failed_count} "
    visible_summary = len(summary_line)
    padding_summary = " " * max(0, 68 - visible_summary)
    print(f"\033[1m\033[96m║\033[0m{summary_line}{padding_summary}\033[1m\033[96m║\033[0m")
    print("\033[1m\033[96m╚" + "═" * 70 + "╝\033[0m\n")