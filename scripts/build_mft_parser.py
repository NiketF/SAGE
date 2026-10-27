"""Build the optional C parser with a Windows x64 MinGW-w64 GCC compiler.

python scripts/build_mft_parser.py --cc C:\path\to\gcc.exe
Only Python's standard library and development headers are used.
"""
import argparse
from pathlib import Path
import struct
import subprocess
import sys
import sysconfig

parser=argparse.ArgumentParser()
parser.add_argument('--cc',default='gcc')
args=parser.parse_args()
if sys.platform!='win32' or struct.calcsize('P')!=8:
    parser.error('Build with Windows x64 Python.')
root=Path(__file__).resolve().parents[1]
folder=root/'src'/'sage'/'platform'/'windows'
output=folder/('_mft_fast'+sysconfig.get_config_var('EXT_SUFFIX'))
library=Path(sys.base_prefix)/'libs'/f'python{sys.version_info.major}{sys.version_info.minor}.lib'
command=[args.cc,'-shared','-O3','-std=c11','-Wall','-Wextra','-Werror','-static-libgcc',
         '-I'+sysconfig.get_path('include'),str(folder/'_mft_fast.c'),str(library),'-o',str(output)]
subprocess.run(command,check=True)
print(f'Built {output}')
