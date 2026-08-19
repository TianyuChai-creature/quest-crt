#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-/tmp/quest-crt-cloudxr}"
video_path="$output_dir/stereo_gate.mp4"
config_path="$output_dir/stereo_gate.yaml"
mkdir -p "$output_dir"

ffmpeg -y -hide_banner -loglevel warning \
    -f lavfi -i "color=c=black:s=2560x720:r=30,drawbox=x=0:y=0:w=1280:h=720:color=0x501818:t=fill,drawbox=x=1280:y=0:w=1280:h=720:color=0x182050:t=fill,drawtext=text='LEFT EYE':fontcolor=white:fontsize=96:x=(1280-text_w)/2:y=70,drawtext=text='Close right eye':fontcolor=white:fontsize=42:x=(1280-text_w)/2:y=190,drawtext=text='RIGHT EYE':fontcolor=white:fontsize=96:x=1280+(1280-text_w)/2:y=70,drawtext=text='Close left eye':fontcolor=white:fontsize=42:x=1280+(1280-text_w)/2:y=190,drawbox=x='mod(t*180\,1120)':y=360:w=160:h=160:color=yellow:t=fill,drawbox=x='1280+mod(t*180+80\,1120)':y=360:w=160:h=160:color=cyan:t=fill" \
    -vf format=yuv420p -t 10 -c:v libx264 -preset veryfast -tune zerolatency \
    -crf 18 -g 30 -bf 0 -movflags +faststart "$video_path"

printf '%s\n' \
    'verbose: true' \
    'source: local' \
    'cameras:' \
    '  - name: stereo_gate' \
    '    enabled: true' \
    '    type: video' \
    "    path: '$video_path'" \
    '    loop: true' \
    '    fps: 30' \
    '    stereo: true' \
    'display:' \
    '  mode: xr' \
    '  xr:' \
    '    near_z: 0.05' \
    '    far_z: 100.0' \
    '  clear_color: [0.02, 0.02, 0.02, 1.0]' \
    '  placements:' \
    '    stereo_gate:' \
    '      lock_mode: lazy' \
    '      distance: 1.0' \
    '      stereo_baseline_mm: 0.0' \
    > "$config_path"

printf 'video=%s\nconfig=%s\n' "$video_path" "$config_path"
