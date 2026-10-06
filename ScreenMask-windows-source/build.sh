#!/bin/sh
# Builds ScreenMask.exe with MinGW-w64 (on Linux: apt install gcc-mingw-w64-x86-64;
# on Windows: MSYS2 "pacman -S mingw-w64-x86_64-gcc", run from the MINGW64 shell
# with CC=gcc WINDRES=windres).
set -e
cd "$(dirname "$0")"
: "${CC:=x86_64-w64-mingw32-gcc}"
: "${WINDRES:=x86_64-w64-mingw32-windres}"
"$WINDRES" screenmask.rc -O coff -o screenmask.res
"$CC" -O2 -Wall -Wextra -municode -mwindows -static -s screenmask.c screenmask.res \
    -o ScreenMask.exe -lcomctl32 -lgdi32 -lshell32 -ldwmapi
rm -f screenmask.res
echo "built $(pwd)/ScreenMask.exe"
