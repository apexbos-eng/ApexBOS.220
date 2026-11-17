# mosfet_auth.py
"""
MIT License
Copyright (c) 2026 AB

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import time
import hmac
import hashlib
import struct

def generate_token(secret_key: str, window_minutes: int = 10, time_offset_seconds: float = 0.0) -> str:
    """
    Generates a time-based authentication token valid for 'window_minutes'.
    Supports time_offset_seconds to generate past or future tokens.
    """
    target_time = time.time() + time_offset_seconds
    window_seed = int(target_time // (window_minutes * 60))
    
    msg = struct.pack(">Q", window_seed)
    h = hmac.new(secret_key.encode('utf-8'), msg, hashlib.sha256).digest()
    return h.hex()[:16]

def get_valid_tokens(secret_key: str, window_minutes: int = 10) -> list:
    """Returns a list of [previous, current, next] tokens to account for clock drift."""
    return [
        generate_token(secret_key, window_minutes, -window_minutes * 60.0), # Previous
        generate_token(secret_key, window_minutes, 0.0),                    # Current
        generate_token(secret_key, window_minutes, window_minutes * 60.0)   # Next
    ]