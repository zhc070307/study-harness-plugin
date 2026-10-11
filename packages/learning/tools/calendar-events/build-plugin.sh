#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macosx14.0" \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$PWD/Plugin-Info.plist" \
  study-calendar.swift -o study-calendar
codesign --force --sign - --identifier com.study-agent.harness-calendar study-calendar
