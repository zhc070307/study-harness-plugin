#!/usr/bin/env bash
# 编译 calendar-events 为原生二进制（需要 Xcode Command Line Tools）
set -euo pipefail
cd "$(dirname "$0")"

minimum_macos="14.0"
build_dir=".build"
mkdir -p "$build_dir"

common_flags=(
  -O
  -swift-version 5
  -module-name CalendarEvents
  -Xlinker -sectcreate
  -Xlinker __TEXT
  -Xlinker __info_plist
  -Xlinker "$PWD/CalendarEvents-Info.plist"
)

for architecture in arm64 x86_64; do
  swiftc "${common_flags[@]}" \
    -target "${architecture}-apple-macosx${minimum_macos}" \
    calendar-events.swift \
    -o "$build_dir/calendar-events-$architecture"
done

lipo -create \
  "$build_dir/calendar-events-arm64" \
  "$build_dir/calendar-events-x86_64" \
  -output "$build_dir/calendar-events-universal"

# 在构建目录中签名，再复制最终产物。
codesign --force --sign - \
  --identifier com.local.calendar-events \
  "$build_dir/calendar-events-universal"
cp "$build_dir/calendar-events-universal" calendar-events
echo "Built: $(pwd)/calendar-events"
