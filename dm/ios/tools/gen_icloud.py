#!/usr/bin/env python3
"""Generate matching iCloud Documents signing entitlements and bundle metadata.

These declarations do not provision a container or grant an unsigned IPA access.
The eventual signer must associate the container with the App ID and include it
in the provisioning profile. The packaged copy documents the required grants.
"""
import argparse
import json
import plistlib
import re
import subprocess
from pathlib import Path


def enable_capability(project):
    # XcodeGen 2.46 serializes nested SystemCapabilities attributes as a string
    # (upstream #1637). Parse with Apple's tool and emit a normal XML plist;
    # never put a malformed capability into the source project template.
    data = json.loads(subprocess.check_output(['plutil', '-convert', 'json', '-o', '-', str(project)], text=True))
    objects = data['objects']
    root_id = data['rootObject']
    root = objects[root_id]
    targets = [target for target in root['targets'] if objects[target].get('name') == 'DmIOS']
    if len(targets) != 1:
        raise ValueError('expected one DmIOS target in the generated project')
    attributes = root.setdefault('attributes', {})
    target = attributes.setdefault('TargetAttributes', {}).setdefault(targets[0], {})
    capabilities = target.setdefault('SystemCapabilities', {})
    if not isinstance(capabilities, dict):
        raise ValueError('generated SystemCapabilities must be a dictionary')
    capabilities['com.apple.iCloud'] = {'enabled': 1}
    project.write_bytes(plistlib.dumps(data))  # Xcode accepts XML project plists; plutil cannot emit OpenStep.


def configure(gen, bundle_id, display_name, container=None, base=None):
    container = container or 'iCloud.com.oops.game'
    if not re.fullmatch(r'iCloud\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*', container):
        raise ValueError('iCloud container must be an explicit iCloud.<identifier>, without wildcards')
    entitlements = dict(base) if base is not None else {'com.apple.developer.kernel.increased-memory-limit': True}
    # An explicit signing override cannot silently request a different container
    # from the one the running app will use. Preserve unrelated capabilities.
    for key in ('com.apple.developer.icloud-container-identifiers',
                'com.apple.developer.ubiquity-container-identifiers'):
        values = entitlements.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f'{key} must be an array of container identifiers')
        entitlements[key] = [container] + [v for v in values if v != container]
    services = entitlements.get('com.apple.developer.icloud-services', [])
    if not isinstance(services, list) or not all(isinstance(v, str) for v in services):
        raise ValueError('com.apple.developer.icloud-services must be an array')
    entitlements['com.apple.developer.icloud-services'] = list(dict.fromkeys(['CloudDocuments'] + services))
    info_path = gen / 'Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info['DMICloudContainerIdentifier'] = container
    info['NSUbiquitousContainers'] = {container: {
        'NSUbiquitousContainerIsDocumentScopePublic': True,
        'NSUbiquitousContainerName': display_name,
        'NSUbiquitousContainerSupportedFolderLevels': 'Any',
    }}
    encoded = plistlib.dumps(entitlements)
    (gen / 'DmIOS.entitlements').write_bytes(encoded)
    (gen / 'dm').mkdir(parents=True, exist_ok=True)
    (gen / 'dm/iCloud.entitlements').write_bytes(encoded)
    info_path.write_bytes(plistlib.dumps(info))
    project = gen.parent / 'DmIOS.xcodeproj/project.pbxproj'
    if project.exists():
        enable_capability(project)
    return container


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gen', type=Path)
    parser.add_argument('--bundle-id', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--container')
    parser.add_argument('--base', type=Path, help='merge a custom entitlements plist')
    parser.add_argument('--no-memory-limit', action='store_true')
    args = parser.parse_args()
    try:
        base = plistlib.loads(args.base.read_bytes()) if args.base else ({} if args.no_memory_limit else None)
        if base is not None and not isinstance(base, dict):
            raise ValueError('custom entitlements must be a dictionary')
        container = configure(args.gen, args.bundle_id, args.name, args.container, base)
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.CalledProcessError) as error:
        parser.exit(1, f'[RED] iCloud configuration: {error}\n')
    print(f'iCloud container: {container}; final signing must provision iCloud Documents access')


if __name__ == '__main__':
    main()
