"""Signing must match the final bundle's cloud container and profile grants."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[3] / 'scripts/sign_ipa.py'
spec = importlib.util.spec_from_file_location('sign_ipa', path)
sign = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sign)


class CloudSigning(unittest.TestCase):
    def test_final_bundle_updates_all_container_fields(self):
        container = 'iCloud.com.oops.game'
        keys = ('com.apple.developer.icloud-container-identifiers', 'com.apple.developer.ubiquity-container-identifiers')
        grants = {k: [container] for k in keys}
        grants['com.apple.developer.icloud-services'] = '*'
        info = {'DMICloudContainerIdentifier': 'iCloud.org.nxnative.old',
                'NSUbiquitousContainers': {'iCloud.org.nxnative.old': {'NSUbiquitousContainerName': 'Game'}}}
        ent = {'unrelated': True}
        self.assertEqual(sign.configure_icloud(info, ent, {'Entitlements': grants}, 'com.oops.totk'), container)
        self.assertEqual(info['DMICloudContainerIdentifier'], container)
        self.assertEqual(list(info['NSUbiquitousContainers']), [container])
        self.assertEqual(ent['com.apple.developer.icloud-services'], ['CloudDocuments'])
        for key in keys:
            self.assertEqual(ent[key], [container])
        self.assertTrue(ent['unrelated'])

    def test_empty_container_profile_is_rejected_before_mutation(self):
        info = {'DMICloudContainerIdentifier': 'iCloud.old'}
        profile = {'Entitlements': {'com.apple.developer.icloud-services': '*',
                   'com.apple.developer.icloud-container-identifiers': [],
                   'com.apple.developer.ubiquity-container-identifiers': []}}
        with self.assertRaisesRegex(SystemExit, 'iCloud.com.oops.game'):
            sign.configure_icloud(info, {}, profile, 'com.oops.totk')
        self.assertEqual(info['DMICloudContainerIdentifier'], 'iCloud.old')

    def test_non_cloud_app_is_unchanged(self):
        info, ent = {}, {}
        self.assertIsNone(sign.configure_icloud(info, ent, None, 'com.example.app'))
        self.assertEqual((info, ent), ({}, {}))

    def test_explicit_shared_container_and_missing_service(self):
        shared = 'iCloud.com.example.shared'
        grants = {k: [shared] for k in ('com.apple.developer.icloud-container-identifiers', 'com.apple.developer.ubiquity-container-identifiers')}
        with self.assertRaisesRegex(SystemExit, 'CloudDocuments'):
            sign.configure_icloud({}, {}, {'Entitlements': grants}, 'com.oops.totk', shared)
        grants['com.apple.developer.icloud-services'] = ['CloudDocuments']
        self.assertEqual(sign.configure_icloud({}, {}, {'Entitlements': grants}, 'com.oops.totk', shared), shared)


if __name__ == '__main__':
    unittest.main()
