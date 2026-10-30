#pragma once
// Guest startup always uses local files; cloud synchronization is an explicit launcher action.
int dm_cloud_prepare(void);
void dm_cloud_committed(const char *path);
#ifdef __OBJC__
#import <Foundation/Foundation.h>
@interface DMCloudSaveStore : NSObject
@property (nonatomic, strong) NSURL *mirror;
- (instancetype)initWithFolder:(NSURL *)folder local:(NSURL *)local title:(NSString *)title;
- (BOOL)prepare:(NSError **)error;
- (BOOL)backupLocal:(NSError **)error;
// Queue a complete slot without waiting for server acknowledgement.
- (NSDictionary *)enqueueBackup:(NSError **)error;
- (NSDictionary *)downloadSnapshot:(NSError **)error;
- (BOOL)capture:(NSError **)error;
- (BOOL)publish:(NSError **)error;
// Overridden only by the local transport regression test.
- (BOOL)checkCloud:(NSURL *)url uploaded:(BOOL)uploaded error:(NSError **)error;
@end
// Call on a worker thread: resolving a provisioned ubiquity container may block.
BOOL dm_cloud_backup_container(NSError **error);
BOOL dm_cloud_restore_container(NSError **error);
void dm_cloud_status_handler(void (^handler)(NSString *, BOOL));
#endif

void dm_cloud_configuration_changed(void);
