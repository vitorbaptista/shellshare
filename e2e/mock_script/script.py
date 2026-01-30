#!/usr/bin/env python3
"""
Mock script binary for testing shellshare CLI's script mode.

This simulates the behavior of the `script` command for testing purposes.
It writes known content to the output file, mimicking a terminal session.

Usage: script [options] <filename>
The mock ignores options and uses the last argument as the filename.

Works on Linux, Mac, and Windows without any external dependencies.
"""

import sys
import time


def main():
    # Get the filename (last argument)
    args = sys.argv[1:]
    
    if not args:
        print("mock script: no output file specified", file=sys.stderr)
        return 1
    
    filename = args[-1]
    
    # Validate filename - skip if it looks like an option
    if filename in ("-qt", "-qf", "0", "-q", "-t", "-f"):
        print("mock script: no output file specified", file=sys.stderr)
        return 1
    
    # Simulate script output by appending to the file
    try:
        with open(filename, "a") as f:
            f.write("MOCK_SCRIPT_START\n")
            f.flush()
            time.sleep(0.2)
            
            f.write("user@host:~$ echo hello\n")
            f.flush()
            time.sleep(0.1)
            
            f.write("hello\n")
            f.flush()
            time.sleep(0.2)
            
            f.write("user@host:~$ whoami\n")
            f.flush()
            time.sleep(0.1)
            
            f.write("testuser\n")
            f.flush()
            time.sleep(0.2)
            
            f.write("user@host:~$ exit\n")
            f.flush()
            time.sleep(0.1)
            
            f.write("MOCK_SCRIPT_END\n")
            f.flush()
        
        # Small delay to ensure file is flushed
        time.sleep(0.3)
        return 0
        
    except IOError as e:
        print(f"mock script: error writing to {filename}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
