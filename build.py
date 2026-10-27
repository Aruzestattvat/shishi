#!/usr/bin/env python3
"""构建「拾事」：编译原生壳、打包 Python 后台、生成图标。

默认只构建到 ./dist/拾事.app；加 --install 才会替换 /Applications/拾事.app。
"""
import argparse
import pathlib
import plistlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent
DEST = pathlib.Path('/Applications/拾事.app')
IDENTIFIER = 'local.shishi.app'
VERSION = '1.9.1'
BUILD = ROOT / '.build'
ICON_SWIFT = '''import Cocoa
let image=NSImage(size:NSSize(width:1024,height:1024))
image.lockFocus()
NSColor(calibratedWhite:0.10,alpha:1).setFill()
NSBezierPath(roundedRect:NSRect(x:25,y:25,width:974,height:974),xRadius:212,yRadius:212).fill()
NSColor(calibratedWhite:0.98,alpha:1).setFill()
NSBezierPath(roundedRect:NSRect(x:240,y:185,width:560,height:650),xRadius:70,yRadius:70).fill()
NSColor(calibratedWhite:0.65,alpha:1).setStroke()
for y in [395,300] { let p=NSBezierPath();p.lineWidth=24;p.lineCapStyle = .round;p.move(to:NSPoint(x:350,y:y));p.line(to:NSPoint(x:670,y:y));p.stroke() }
NSColor(calibratedWhite:0.10,alpha:1).setStroke()
let p=NSBezierPath();p.lineWidth=55;p.lineCapStyle = .round;p.lineJoinStyle = .round;p.move(to:NSPoint(x:355,y:600));p.line(to:NSPoint(x:455,y:510));p.line(to:NSPoint(x:675,y:715));p.stroke()
image.unlockFocus()
let data=NSBitmapImageRep(data:image.tiffRepresentation!)!.representation(using:.png,properties:[:])!
try! data.write(to:URL(fileURLWithPath:CommandLine.arguments[1]))
'''


def run(*args):
    subprocess.run(args, check=True)


def sdk():
    preferred = pathlib.Path('/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk')
    if preferred.exists():
        return str(preferred)
    out = subprocess.run(['xcrun', '--show-sdk-path'], capture_output=True, text=True, check=True).stdout.strip()
    if not out:
        raise SystemExit('找不到可用的 macOS SDK')
    return out


def build(install=False):
    SDK = sdk()
    if DEST.exists():
        info = plistlib.loads((DEST / 'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') != IDENTIFIER:
            raise SystemExit('已有不同的拾事应用，未覆盖')
    BUILD.mkdir(exist_ok=True)
    module_cache = ROOT / '.modulecache'
    module_cache.mkdir(exist_ok=True)
    stage = BUILD / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    app = stage / '拾事.app'
    contents = app / 'Contents'
    res = contents / 'Resources'
    exe = contents / 'MacOS'
    res.mkdir(parents=True)
    exe.mkdir()
    run('swiftc', '-O', '-sdk', SDK, '-module-cache-path', str(module_cache),
        '-framework', 'Cocoa', '-framework', 'WebKit', '-framework', 'EventKit',
        str(ROOT / 'App.swift'), '-o', str(exe / 'Shishi'))
    for name in ('core.py', 'server.py', 'index.html', 'asr.py'):
        shutil.copy2(ROOT / name, res / name)

    iconset = BUILD / 'AppIcon.iconset'
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    icon_script = BUILD / 'Icon.swift'
    icon_script.write_text(ICON_SWIFT)
    icon_png = BUILD / 'icon.png'
    run('swift', '-sdk', SDK, '-module-cache-path', str(module_cache), str(icon_script), str(icon_png))
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = 'icon_%dx%d%s.png' % (size, size, '@2x' if scale == 2 else '')
            subprocess.run(['sips', '-z', str(size * scale), str(size * scale), str(icon_png),
                            '--out', str(iconset / name)], check=True, stdout=subprocess.DEVNULL)
    run('iconutil', '-c', 'icns', str(iconset), '-o', str(res / 'AppIcon.icns'))
    shutil.copy2(icon_png, ROOT / 'icon.png')

    info = {
        'CFBundleIdentifier': IDENTIFIER, 'CFBundleName': '拾事', 'CFBundleDisplayName': '拾事',
        'CFBundleExecutable': 'Shishi', 'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': VERSION, 'CFBundleVersion': '1',
        'CFBundleIconFile': 'AppIcon', 'LSMinimumSystemVersion': '14.0',
        'CFBundleDevelopmentRegion': 'zh_CN', 'NSPrincipalClass': 'NSApplication',
        'NSHighResolutionCapable': True,
        'LSApplicationCategoryType': 'public.app-category.productivity',
        'LSUIElement': True,
        'NSRemindersFullAccessUsageDescription': '拾事会将你在所选微信聊天中明确答应的安排加入指定提醒清单，保留来源，并识别已完成的事项。选择 iCloud 清单可由系统跨设备同步。',
        'NSRemindersUsageDescription': '记录微信中你明确答应的事。',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    }
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    (contents / 'PkgInfo').write_bytes(b'APPL????')
    run('codesign', '--force', '--sign', '-', str(app))
    run('codesign', '--verify', '--strict', str(app))

    dist = ROOT / 'dist' / '拾事.app'
    if dist.exists():
        shutil.rmtree(dist)
    dist.parent.mkdir(exist_ok=True)
    shutil.copytree(app, dist)
    print('Built:', dist)
    if install:
        if DEST.exists():
            backup = ROOT / '.previous.app'
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(DEST), str(backup))
        shutil.copytree(dist, DEST)
        run('/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister',
            '-f', str(DEST))
        print('Installed:', DEST)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true', help='构建后安装到 /Applications/拾事.app')
    args = parser.parse_args()
    build(install=args.install)
