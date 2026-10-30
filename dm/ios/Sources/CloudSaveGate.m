// Optional cloud migration screen. Continuing uses local saves without contacting iCloud.
#import <UIKit/UIKit.h>
#import "cloud_save.h"

@interface DMCloudSaveGate : UIViewController
@property (copy) void (^done)(void);
@property (strong) UILabel *message;
@property (strong) UIButton *retry;
@property (strong) UIButton *restore;
@property (strong) UIActivityIndicatorView *spinner;
@property BOOL busy;
@property (strong) UIButton *play;
@end

@implementation DMCloudSaveGate
- (void)viewDidLoad {
    [super viewDidLoad]; self.view.backgroundColor = UIColor.systemBackgroundColor;
    UILabel *title = [UILabel new]; title.text = @"iCloud saves";
    title.font = [UIFont preferredFontForTextStyle:UIFontTextStyleTitle1]; title.adjustsFontForContentSizeCategory = YES;
    self.message = [UILabel new]; self.message.numberOfLines = 0;
    self.message.font = [UIFont preferredFontForTextStyle:UIFontTextStyleBody]; self.message.adjustsFontForContentSizeCategory = YES;
    self.retry = [UIButton buttonWithType:UIButtonTypeSystem];
    self.retry.configuration = [UIButtonConfiguration filledButtonConfiguration];
    [self.retry addTarget:self action:@selector(backup) forControlEvents:UIControlEventTouchUpInside];
    [self.retry.heightAnchor constraintGreaterThanOrEqualToConstant:44].active = YES;
    [self.retry setTitle:@"Back up to iCloud" forState:UIControlStateNormal];
    self.restore = [UIButton buttonWithType:UIButtonTypeSystem];
    self.restore.configuration = [UIButtonConfiguration filledButtonConfiguration];
    [self.restore setTitle:@"Restore iCloud backup to this device" forState:UIControlStateNormal];
    [self.restore addTarget:self action:@selector(restoreBackup) forControlEvents:UIControlEventTouchUpInside];
    [self.restore.heightAnchor constraintGreaterThanOrEqualToConstant:44].active = YES;
    self.play = [UIButton buttonWithType:UIButtonTypeSystem];
    self.play.configuration = [UIButtonConfiguration filledButtonConfiguration];
    [self.play setTitle:@"Continue with local saves" forState:UIControlStateNormal];
    [self.play addTarget:self action:@selector(continueLocal) forControlEvents:UIControlEventTouchUpInside];
    [self.play.heightAnchor constraintGreaterThanOrEqualToConstant:44].active = YES;
    self.message.text = @"Games read Documents/save on this device. Back up Documents/save and Documents/cheats to iCloud, or restore those two folders here. Other local files are untouched. Previous folders are preserved in Library/Application Support/RestoreBackups. Playing does not change the cloud backup.";
    self.spinner = [[UIActivityIndicatorView alloc] initWithActivityIndicatorStyle:UIActivityIndicatorViewStyleMedium];
    UIStackView *stack = [[UIStackView alloc] initWithArrangedSubviews:@[title, self.message, self.spinner, self.retry, self.restore, self.play]];
    stack.axis = UILayoutConstraintAxisVertical; stack.spacing = 16; stack.translatesAutoresizingMaskIntoConstraints = NO;
    UIScrollView *scroll = [UIScrollView new]; scroll.translatesAutoresizingMaskIntoConstraints = NO;
    [self.view addSubview:scroll]; [scroll addSubview:stack];
    [NSLayoutConstraint activateConstraints:@[
        [scroll.leadingAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.leadingAnchor constant:24],
        [scroll.trailingAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.trailingAnchor constant:-24],
        [scroll.topAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.topAnchor constant:16],
        [scroll.bottomAnchor constraintEqualToAnchor:self.view.safeAreaLayoutGuide.bottomAnchor constant:-16],
        [stack.leadingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.leadingAnchor],
        [stack.trailingAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.trailingAnchor],
        [stack.topAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.topAnchor],
        [stack.bottomAnchor constraintEqualToAnchor:scroll.contentLayoutGuide.bottomAnchor],
        [stack.widthAnchor constraintEqualToAnchor:scroll.frameLayoutGuide.widthAnchor]
    ]];
}
- (void)continueLocal { if (!self.busy) [self dismissViewControllerAnimated:NO completion:self.done]; }
- (void)backup { [self transfer:NO]; }
- (void)restoreBackup { [self transfer:YES]; }
- (void)transfer:(BOOL)restoring {
    if (self.busy) return;
    self.busy = YES; self.retry.enabled = NO; self.restore.enabled = NO; self.play.enabled = NO;
    self.message.text = restoring ? @"Downloading and restoring the iCloud backup…" : @"Backing up save and cheats to iCloud…"; [self.spinner startAnimating];
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), ^{
        NSError *error; BOOL ready = (restoring ? dm_cloud_restore_container(&error) : dm_cloud_backup_container(&error));
        dispatch_async(dispatch_get_main_queue(), ^{
            self.busy = NO; self.play.enabled = YES; self.retry.enabled = YES; self.restore.enabled = YES; self.retry.hidden = NO; [self.spinner stopAnimating];
            if (ready) self.message.text = (restoring ? @"Backup restored to this device. Previous save and cheats folders are in Library/Application Support/RestoreBackups." : @"Complete backup submitted to iCloud Drive. Upload is handled by iCloud and has not been confirmed complete. Local saves were kept. Wait for iCloud Drive to finish syncing before restoring on another device.");
            else self.message.text = error.localizedDescription ?: @"iCloud is not ready. The game has not started. Retry when syncing finishes.";
        });
    });
}
@end

void dm_cloud_show_gate(UIViewController *parent, void (^done)(void)) {
    DMCloudSaveGate *gate = [DMCloudSaveGate new]; gate.done = done;
    gate.modalPresentationStyle = UIModalPresentationFullScreen;
    [parent presentViewController:gate animated:NO completion:nil];
}
