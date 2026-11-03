#!/bin/bash
# ============================================================================
#  TouchBridge 编译脚本（触控板 → OSC 采集器）
# ----------------------------------------------------------------------------
#  作用：把 main.swift 编译成可执行文件，并打包成 TouchBridge.app。
#  要求：macOS + 命令行工具（Xcode Command Line Tools）
#         没装的话先在终端执行：  xcode-select --install
#
#  为什么用源码编译而不是发预编译包？
#    - 预编译的 .app 是某一台机器的架构（arm64 / x86_64）+ 签名，换台 Mac
#      十有八九被 Gatekeeper 拦、或架构不对跑不起来。
#    - 源码用 swiftc 在目标 Mac 上编译，二进制会匹配本机 CPU 架构；
#    - 触控板读取依赖未公开的 MultitouchSupport，不能保证所有机型与
#      macOS 版本都兼容。当前实机验证环境见仓库 README。
#
#  用法：  bash build.sh
# ============================================================================
set -e

cd "$(dirname "$0")"

echo "▶ 编译 main.swift ..."
# swiftc 会自动链接 import 的系统框架（AppKit / CoreFoundation / CoreText / QuartzCore）
swiftc main.swift -o TouchBridge
echo "  ✔ 已生成可执行文件：TouchBridge"

echo "▶ 打包为 TouchBridge.app ..."
APP="TouchBridge.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
cp TouchBridge "$APP/Contents/MacOS/TouchBridge"
cp Info.plist "$APP/Contents/Info.plist"

# 本地自签名（ad-hoc，签名标识用 "-")，能减少首次打开时的 Gatekeeper 拦截。
# 如果你之后要长期分发，建议用自己的开发者证书签名；没有也完全不影响本地使用。
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP" 2>/dev/null && echo "  ✔ 已本地自签名" \
    || echo "  （codesign 跳过，不影响使用）"
fi

echo ""
echo "✔ 完成：$APP"
echo ""
echo "  首次打开若 macOS 提示“无法验证开发者 / 已被拦截”，任选其一解除："
echo "    1) 右键点击 TouchBridge.app → 打开（仅首次需要）"
echo "    2) 终端执行：  xattr -cr \"$PWD/$APP\""
echo ""
echo "  无界面自检（不需要 GUI，验证 OSC 能否发出）："
echo "    ./TouchBridge.app/Contents/MacOS/TouchBridge --osctest"
