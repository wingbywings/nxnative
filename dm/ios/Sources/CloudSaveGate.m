// Worker bridge for the optional backup/restore rows in Launcher.swift.
#import <Foundation/Foundation.h>
#import "cloud_save.h"

// Completion is always delivered on the main queue. No cloud access occurs
// until the launcher confirms an explicit action; missing entitlements only
// fail that action and never prevent local play.
void dm_launcher_cloud_transfer(int restoring, void (^completion)(NSString *)) {
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSError *error;
        BOOL ok = restoring ? dm_cloud_restore_container(&error) : dm_cloud_backup_container(&error);
        NSString *message;
        if (ok) message = restoring
            ? @"Save and cheats restored. Previous copies are retained in Library/Application Support/RestoreBackups."
            : @"Save and cheats submitted to iCloud Drive. Upload is not yet confirmed complete. Wait for iCloud to finish syncing before restoring on another device.";
        else message = [NSString stringWithFormat:@"%@ You can still play using local saves.", error.localizedDescription ?: @"iCloud is unavailable."];
        dispatch_async(dispatch_get_main_queue(), ^{ completion(message); });
    });
}
