#!/usr/bin/env python3
#
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

import sys
import os
import json
import argparse
import re
from copy import deepcopy
from parse_packets import PacketParser
from version import VERSION, __version__

# Cross-platform raw input
if sys.platform != 'win32':
    import select
    import tty
    import termios
    def getch():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        if ch == '\x03':  # Ctrl+C
            raise KeyboardInterrupt
        return ch
        
    def read_key():
        """Reads a single key, handling ANSI escape sequences for arrows and standalone ESC."""
        ch = getch()
        if ch == '\x1b':  # Unix escape sequence or ESC
            dr, _, _ = select.select([sys.stdin], [], [], 0.01)
            if dr:
                ch2 = getch()
                if ch2 == '[':
                    dr2, _, _ = select.select([sys.stdin], [], [], 0.01)
                    if dr2:
                        ch3 = getch()
                        if ch3 == 'A': return "UP"
                        if ch3 == 'B': return "DOWN"
                        if ch3 == 'C': return "RIGHT"
                        if ch3 == 'D': return "LEFT"
                return ""
            else:
                return "ESC"
        elif ch == '\x04': # Ctrl-D
            return "CTRL_D"
        return ch
else:
    import msvcrt
    def getch():
        ch = msvcrt.getch()
        try:
            ch = ch.decode('utf-8', 'ignore')
        except:
            pass
        if ch == '\x03':  # Ctrl+C
            raise KeyboardInterrupt
        return ch
        
    def read_key():
        """Reads a single key for Windows."""
        ch = getch()
        if ch == '\x1b':
            return "ESC"
        elif ch == '\x04':
            return "CTRL_D"
        elif ch in ('\xe0', b'\xe0', '\x00', b'\x00'):  # Windows escape sequence for arrows
            ch2 = getch()
            if isinstance(ch2, bytes):
                ch2 = ch2.decode('utf-8', 'ignore')
            if ch2 == 'H': return "UP"
            if ch2 == 'P': return "DOWN"
            if ch2 == 'M': return "RIGHT"
            if ch2 == 'K': return "LEFT"
            return ""
        return ch

# ANSI Color Codes
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def clear_screen():
    print("\033[2J\033[H", end="")

def display_hex_ascii(data: bytes, parsed_bits: int):
    parsed_bytes = (parsed_bits + 7) // 8
    
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_str = ""
        ascii_str = ""
        
        for j, b in enumerate(chunk):
            idx = i + j
            color = GREEN if idx < parsed_bytes else YELLOW
            
            hex_str += f"{color}{b:02X}{RESET} "
            char = chr(b) if 32 <= b <= 126 else "."
            ascii_str += f"{color}{char}{RESET}"
            
        padding = " " * (3 * (16 - len(chunk)))
        print(f"{CYAN}{i:04X}{RESET}  {hex_str}{padding}  {ascii_str}")
    print()

def read_field(prompt: str, history: list) -> tuple:
    """Reads a field value. Returns (value, status) where status is 'OK', 'CANCEL', or 'COMPLETE'."""
    sys.stdout.write(f"{BOLD}{prompt}:{RESET} ")
    sys.stdout.flush()
    val = ""
    history_idx = len(history)
    
    while True:
        key = read_key()
        if key == "ESC":
            sys.stdout.write('\n')
            return ("", "CANCEL")
        elif key == "CTRL_D":
            sys.stdout.write('\n')
            v = val.strip()
            if v: history.append(v)
            return (v, "COMPLETE")
        elif key in ('\r', '\n', '\t'):
            sys.stdout.write('\n')
            v = val.strip()
            if v: history.append(v)
            return (v, "OK")
        elif key in ('\x08', '\x7f'):  # Backspace
            if len(val) > 0:
                val = val[:-1]
                sys.stdout.write('\b \b')
                sys.stdout.flush()
        elif key == "UP":
            if history_idx > 0:
                history_idx -= 1
                sys.stdout.write('\b \b' * len(val))
                val = history[history_idx]
                sys.stdout.write(val)
                sys.stdout.flush()
        elif key == "DOWN":
            if history_idx < len(history):
                sys.stdout.write('\b \b' * len(val))
                history_idx += 1
                if history_idx == len(history):
                    val = ""
                else:
                    val = history[history_idx]
                sys.stdout.write(val)
                sys.stdout.flush()
        elif len(key) == 1 and ord(key) >= 32:
            val += key
            sys.stdout.write(key)
            sys.stdout.flush()

def read_type(history: list) -> tuple:
    """Reads type. Returns (value, status) where status is 'OK', 'CANCEL', or 'COMPLETE'."""
    types = [
        'u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'u64', 'i64', 
        'f16', 'f32', 'f64', 'f', 's', 's0', 'a', 'b', 'k', 'd', 'b1', 'bl', 'ib', 'cp',
        'uint8', 'int8', 'uint16', 'int16', 'uint32', 'int32', 'uint64', 'int64',
        'string', 'string_0', 'array', 'branch', 'skip', 'bcd', 'bit', 'bool', 'int_bits', 'cpychk',
        'section', 'end_section', 'printf'
    ]
    val = ""
    history_idx = len(history)
    
    while True:
        matches = [t for t in types if t.startswith(val)]
        hint = ", ".join(matches) if matches else "No match"
        
        sys.stdout.write(f"\r\033[2K{BOLD}Type:{RESET} {val}  {CYAN}(Hints: {hint}){RESET}")
        sys.stdout.flush()
        
        if len(matches) == 1 and val == matches[0]:
            sys.stdout.write(f"\r\033[2K{BOLD}Type:{RESET} {val}\n")
            if val: history.append(val)
            return (val, "OK")
            
        key = read_key()
        if key == "ESC":
            sys.stdout.write('\n')
            return ("", "CANCEL")
        elif key == "CTRL_D":
            ret = matches[0] if matches else val
            sys.stdout.write(f"\r\033[2K{BOLD}Type:{RESET} {ret}\n")
            if ret: history.append(ret)
            return (ret, "COMPLETE")
        elif key in ('\r', '\n', '\t'):
            ret = matches[0] if matches else val
            sys.stdout.write(f"\r\033[2K{BOLD}Type:{RESET} {ret}\n")
            if ret: history.append(ret)
            return (ret, "OK")
        elif key in ('\x08', '\x7f'):
            if len(val) > 0:
                val = val[:-1]
        elif key == "UP":
            if history_idx > 0:
                history_idx -= 1
                val = history[history_idx]
        elif key == "DOWN":
            if history_idx < len(history):
                history_idx += 1
                val = "" if history_idx == len(history) else history[history_idx]
        elif len(key) == 1 and ord(key) >= 32:
            val += key

def get_allowed_fields(r_type: str) -> list:
    fields = []
    # end_section doesn't take parameters, skip takes length (and usually no name)
    if r_type not in ['skip', 'k', 'end_section']:
        fields.append('name')
        
    if r_type in ['s', 'string', 'd', 'bcd', 'ib', 'int_bits', 'skip', 'k', 'a', 'array', 'cpychk', 'cp']:
        fields.append('length')
        
    if r_type in ['a', 'array']:
        fields.append('item_type')
        
    if r_type == 'printf':
        fields.append('format')
        
    # Standard format options not needed for structural or special evaluation rules
    if r_type not in ['skip', 'k', 'b', 'branch', 'a', 'array', 'cpychk', 'cp', 'section', 'end_section', 'printf']:
        fields.extend(['expected', 'format', 'lookup (JSON dict)'])
        
    return fields

def make_unique_name(base_name: str, rules: list) -> str:
    existing = [r.get('name') for r in rules if 'name' in r]
    if base_name not in existing:
        return base_name
        
    m = re.search(r'(\d+)$', base_name)
    if m:
        prefix = base_name[:m.start()]
        num = int(m.group(1))
    else:
        prefix = base_name + "_"
        num = 1
        
    while f"{prefix}{num}" in existing:
        num += 1
    return f"{prefix}{num}"

def convert_to_alt_format(rules: list) -> list:
    """Converts internal parsing rules into the alternative itcmon format."""
    
    len_fields_to_omit = set()
    
    def scan_for_dynamic_lengths(rs):
        for r in rs:
            r_type = r.get("type", "")
            if r_type in ['string', 's', 'array', 'a']:
                length = r.get("length")
                if isinstance(length, str) and not length.isdigit():
                    len_fields_to_omit.add(length)
            elif r_type in ['branch', 'b']:
                scan_for_dynamic_lengths(r.get("rules", []))
                
    scan_for_dynamic_lengths(rules)
    
    def process_rules(rs):
        alt_rules = []
        for r in rs:
            name = r.get("name", "X")
            
            # Drop the rule if it was just used to size a dynamic string/array
            if name in len_fields_to_omit:
                continue
                
            r_type = r.get("type", "")
            length = r.get("length")
            fmt = r.get("format", "")

            # Ignore structure-only or unparseable types for alt format
            if r_type in ['section', 'end_section', 'printf']:
                continue

            if r_type in ['branch', 'b']:
                cond = str(r.get("condition", ""))
                m = re.match(r'\s*(\w+)\s*==\s*([\w\d]+)\s*', cond)
                if m:
                    sel_field = m.group(1)
                    val = m.group(2)
                    alt_rules.append({
                        "select": f"{sel_field}:*:x",
                        "match": {
                            val: process_rules(r.get("rules", []))
                        }
                    })
                else:
                    alt_rules.append({"field": "Branch_Unconverted:*:x"})
                continue

            bits = "*"
            if r_type in ['u8', 'i8', 'uint8', 'int8']: bits = "8"
            elif r_type in ['u16', 'i16', 'uint16', 'int16']: bits = "16"
            elif r_type in ['u32', 'i32', 'uint32', 'int32']: bits = "32"
            elif r_type in ['u64', 'i64', 'uint64', 'int64']: bits = "64"
            elif r_type in ['bit', 'bool', 'b1', 'bl']: bits = "1"
            elif r_type in ['int_bits', 'ib', 'd', 'bcd']:
                bits = str(length) if length is not None else "*"
            elif r_type in ['string', 's', 'array', 'a']:
                try:
                    bits = str(int(length) * 8)
                except (ValueError, TypeError):
                    bits = "*"
            elif r_type in ['string_0', 's0']:
                bits = "0"
            elif r_type in ['skip', 'k']:
                name = "Skip"
                try:
                    bits = str(int(length) * 8)
                except (ValueError, TypeError):
                    bits = "*"
            elif r_type in ['cpychk', 'cp']:
                try:
                    bits = str(int(length))
                except (ValueError, TypeError):
                    bits = "*"

            alt_fmt = fmt
            if not alt_fmt:
                if r_type in ['string', 's', 'string_0', 's0']:
                    alt_fmt = "s"
                elif r_type in ['skip', 'k', 'cpychk', 'cp']:
                    alt_fmt = "x"
                else:
                    alt_fmt = "d"

            alt_rules.append({"field": f"{name}:{bits}:{alt_fmt}"})
            
        return alt_rules

    return process_rules(rules)

def save_rules_file(filename: str, rules: list, use_alt: bool) -> str:
    """Handles saving to file, applying itcmon conversion and prefix if requested."""
    if use_alt:
        d, f = os.path.split(filename)
        if not f.startswith("itcmon_"):
            f = "itcmon_" + f
            filename = os.path.join(d, f) if d else f
        save_data = {"rules": convert_to_alt_format(rules)}
    else:
        save_data = rules
        
    with open(filename, 'w') as f_out:
        json.dump(save_data, f_out, indent=4)
        
    return filename

def main():
    parser_args = argparse.ArgumentParser(description=f"PackFIG ({VERSION}) - Interactive binary packet decoder.")
    parser_args.add_argument('packet', nargs='?', help='Hex string of the packet to decode (positional)')
    parser_args.add_argument('-x', '--hex', help='Hex string of the packet to decode')
    parser_args.add_argument('-f', '--file', help='Load binary packet from file')
    parser_args.add_argument('-r', '--rules', help='Load rules from JSON file')
    parser_args.add_argument('-a', '--alt', action='store_true', help='Save files with itcmon prefix and alternate format')
    args = parser_args.parse_args()

    packet_data = b''
    if args.file:
        try:
            with open(args.file, 'rb') as f:
                packet_data = f.read()
        except IOError as e:
            print(f"Error reading file: {e}")
            sys.exit(1)
    elif args.hex:
        try:
            packet_data = bytes.fromhex(args.hex.strip().replace(" ", "").replace("\n", ""))
        except ValueError:
            print("Invalid hex string provided in --hex argument.")
            sys.exit(1)
    elif args.packet:
        try:
            packet_data = bytes.fromhex(args.packet.strip().replace(" ", "").replace("\n", ""))
        except ValueError:
            print("Invalid hex string provided in positional argument.")
            sys.exit(1)

    if not packet_data:
        clear_screen()
        print(f"{BOLD}Interactive Packet Decoder{RESET}")
        print("Enter the hex string of the packet to decode:")
        sys.stdout.write("> ")
        sys.stdout.flush()
        try:
            hex_input = input().strip().replace(" ", "").replace("\n", "")
            packet_data = bytes.fromhex(hex_input)
        except ValueError:
            print("Invalid hex string provided. Exiting.")
            sys.exit(1)
        except KeyboardInterrupt:
            print("\nExiting UI.")
            sys.exit(0)

    rules = []
    rules_dirty = False
    current_filename = None

    if args.rules:
        try:
            with open(args.rules, 'r') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and "rules" in loaded:
                    rules = loaded["rules"]
                else:
                    rules = loaded
            current_filename = args.rules
        except Exception as e:
            print(f"Error loading rules from {args.rules}: {e}")
            sys.exit(1)
    
    # Independent histories per field
    field_histories = {
        'type': [],
        'name': [],
        'length': [],
        'item_type': [],
        'expected': [],
        'format': [],
        'lookup (JSON dict)': []
    }

    try:
        while True:
            clear_screen()
            
            parser = PacketParser(rules)
            parser.parse(packet_data)
            
            display_hex_ascii(packet_data, parser.bit_pos)
            parser.display_formatted()
            
            file_disp = current_filename if current_filename else "Unsaved"
            print(f"\n{CYAN}{BOLD}--- Control Menu (File: {file_disp}) ---{RESET}")
            print(f"[{GREEN}A{RESET}]dd Rule   [{YELLOW}U{RESET}]ndo   [{YELLOW}P{RESET}]epeat Last   [{CYAN}S{RESET}]ave   Sa[{CYAN}V{RESET}]e As   [{CYAN}L{RESET}]oad   [{CYAN}R{RESET}]eset   [{CYAN}Q{RESET}]uit")
            
            sys.stdout.write(f"\nSelect action: ")
            sys.stdout.flush()
            
            browse_idx = len(rules)
            selected_rule_to_repeat = None
            
            # Immediate keystroke detection (no Enter required)
            while True:
                choice = read_key()
                if choice == "UP":
                    if rules and browse_idx > 0:
                        browse_idx -= 1
                        r = rules[browse_idx]
                        r_str = f"type={r.get('type')} name={r.get('name', 'N/A')}"
                        sys.stdout.write(f"\r\033[2KSelect action: [History: {r_str}] ")
                        sys.stdout.flush()
                elif choice == "DOWN":
                    if rules and browse_idx < len(rules) - 1:
                        browse_idx += 1
                        r = rules[browse_idx]
                        r_str = f"type={r.get('type')} name={r.get('name', 'N/A')}"
                        sys.stdout.write(f"\r\033[2KSelect action: [History: {r_str}] ")
                        sys.stdout.flush()
                    elif rules and browse_idx == len(rules) - 1:
                        browse_idx += 1
                        sys.stdout.write(f"\r\033[2KSelect action: ")
                        sys.stdout.flush()
                elif choice in ('\r', '\n'):
                    if browse_idx < len(rules):
                        selected_rule_to_repeat = deepcopy(rules[browse_idx])
                        choice = 'H'
                        print("Enter")
                        break
                elif len(choice) == 1 and choice.isalpha():
                    choice = choice.upper()
                    if choice in ['A', 'U', 'P', 'S', 'V', 'L', 'R', 'Q']:
                        print(choice)
                        break

            if choice == 'A':
                print(f"\n{CYAN}--- New Parsing Rule (Use Tab/Enter to skip, Up/Down for history, Ctrl-D to complete early, ESC to cancel) ---{RESET}")
                r_type, status = read_type(field_histories['type'])
                if status == "CANCEL" or not r_type:
                    print(f"{YELLOW}Entry cancelled.{RESET}")
                    continue
                    
                new_rule = {"type": r_type}
                allowed_fields = get_allowed_fields(r_type)
                cancelled = False
                
                if status != "COMPLETE":
                    for field in allowed_fields:
                        val, status = read_field(field, field_histories[field])
                        if status == "CANCEL":
                            cancelled = True
                            break
                        
                        if val:
                            if field == 'length':
                                new_rule['length'] = int(val) if val.isdigit() else val
                            elif field == 'expected':
                                new_rule['expected'] = int(val, 0) if val.startswith("0x") or val.isdigit() else val
                            elif field == 'lookup (JSON dict)':
                                try:
                                    new_rule['lookup'] = json.loads(val)
                                except json.JSONDecodeError:
                                    print(f"{YELLOW}Warning: Invalid JSON for lookup. Skipped.{RESET}")
                            else:
                                new_rule[field] = val
                                
                        if status == "COMPLETE":
                            break
                            
                if cancelled:
                    print(f"{YELLOW}Entry cancelled.{RESET}")
                    continue
                
                if 'name' in allowed_fields:
                    if 'name' not in new_rule:
                        new_rule['name'] = f"field_{len(rules)}"
                    new_rule['name'] = make_unique_name(new_rule['name'], rules)
                    
                rules.append(new_rule)
                rules_dirty = True
                
            elif choice in ('P', 'H'):
                if rules or choice == 'H':
                    print(f"\n{CYAN}--- Repeat Rule (Ctrl-D to complete early, ESC to cancel) ---{RESET}")
                    if choice == 'P':
                        new_rule = deepcopy(rules[-1])
                    else:
                        new_rule = selected_rule_to_repeat
                    
                    if 'name' in new_rule:
                        old_name = new_rule['name']
                        new_name, status = read_field(f"New name (was {old_name})", field_histories['name'])
                        
                        if status == "CANCEL":
                            print(f"{YELLOW}Entry cancelled.{RESET}")
                            continue
                            
                        if new_name:
                            new_rule['name'] = make_unique_name(new_name, rules)
                        else:
                            new_rule['name'] = make_unique_name(old_name, rules)
                            
                    rules.append(new_rule)
                    rules_dirty = True
                    
            elif choice == 'U':
                if rules:
                    last_rule_name = rules[-1].get('name', rules[-1].get('type', 'rule'))
                    sys.stdout.write(f"\n{YELLOW}Undo last rule '{last_rule_name}'? (y/N): {RESET}")
                    sys.stdout.flush()
                    while True:
                        ans = read_key()
                        if ans.lower() in ('y', 'n', '\r', '\n', 'ESC'):
                            if ans in ('\r', '\n', 'ESC'):
                                ans = 'n'
                            print(ans)
                            break
                    if ans.lower() == 'y':
                        rules.pop()
                        rules_dirty = True
                    
            elif choice == 'S':
                if current_filename:
                    filename = current_filename
                else:
                    sys.stdout.write("\nSave to file (e.g. rules.json): ")
                    sys.stdout.flush()
                    filename = input().strip()
                
                if filename:
                    try:
                        current_filename = save_rules_file(filename, rules, args.alt)
                        print(f"\nSaved to {current_filename}")
                        rules_dirty = False
                        read_key()
                    except Exception as e:
                        print(f"\nError saving: {e}")
                        read_key()
                        
            elif choice == 'V':
                prompt_txt = f"\nSave As (current: {current_filename}): " if current_filename else "\nSave As (e.g. rules.json): "
                sys.stdout.write(prompt_txt)
                sys.stdout.flush()
                filename = input().strip()
                if filename:
                    try:
                        current_filename = save_rules_file(filename, rules, args.alt)
                        print(f"\nSaved to {current_filename}")
                        rules_dirty = False
                        read_key()
                    except Exception as e:
                        print(f"\nError saving: {e}")
                        read_key()

            elif choice == 'L':
                sys.stdout.write("\nLoad from file (e.g. rules.json): ")
                sys.stdout.flush()
                filename = input().strip()
                if filename:
                    try:
                        with open(filename, 'r') as f:
                            loaded = json.load(f)
                            if isinstance(loaded, dict) and "rules" in loaded:
                                rules = loaded["rules"]
                            else:
                                rules = loaded
                        current_filename = filename
                        rules_dirty = False
                    except Exception as e:
                        print(f"\nError loading: {e}")
                        read_key()
                        
            elif choice == 'R':
                sys.stdout.write(f"\n{YELLOW}Are you sure you want to reset all rules? (y/N): {RESET}")
                sys.stdout.flush()
                while True:
                    ans = read_key()
                    if ans.lower() in ('y', 'n', '\r', '\n', 'ESC'):
                        if ans in ('\r', '\n', 'ESC'):
                            ans = 'n'
                        print(ans)
                        break
                if ans.lower() == 'y':
                    rules.clear()
                    rules_dirty = True
                
            elif choice == 'Q':
                break
                
    except KeyboardInterrupt:
        pass  # Graceful exit trigger

    # Check dirty flag before actually exiting
    if rules_dirty and rules:
        sys.stdout.write(f"\n\n{YELLOW}Unsaved rules exist. Save before exiting? (Y/n): {RESET}")
        sys.stdout.flush()
        try:
            while True:
                ans = read_key()
                if ans.lower() in ('y', 'n', '\r', '\n', 'ESC'):
                    if ans in ('\r', '\n', 'ESC'):
                        ans = 'y'
                    print(ans)
                    break
                    
            if ans.lower() == 'y':
                prompt_str = f"Save to file [{current_filename}]: " if current_filename else "Save to file (e.g. rules.json): "
                sys.stdout.write(prompt_str)
                sys.stdout.flush()
                filename = input().strip()
                if not filename and current_filename:
                    filename = current_filename
                    
                if filename:
                    try:
                        save_rules_file(filename, rules, args.alt)
                        print(f"Saved to {filename}")
                    except Exception as e:
                        print(f"Error saving: {e}")
        except KeyboardInterrupt:
            pass
            
    clear_screen()
    print("Exiting UI.")

if __name__ == "__main__":
    main()