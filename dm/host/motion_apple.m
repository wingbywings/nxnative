// Real six-axis data for hle_hid.c's rings: a connected controller's GCControllerMotion (about 100 Hz) per player, and on iOS the device's own CMDeviceMotion (100 Hz; needs
// NSMotionUsageDescription, one Motion & Fitness prompt at first start) through dm_motion_device -- the host picks the source per read, the controller's IMU first, the device's
// when the controller has none. A stream whose gravity never reaches 0.5 G is a controller without an IMU as far as GameController reports: its samples are not published, so
// the device's sensors stay in effect (and on the Mac, the sensor falls back to lying flat).
// The quaternion handed over is the conjugate of Apple's attitude: Apple's rotates reference vectors into the device's frame, the host's direction matrix holds the body's axes
// in the world. acc = userAcceleration + gravity in G, gyro in rad/s.
#import "motion_apple.h"
#import "dm.h"
#import "dm_input.h"
#import <TargetConditionals.h>
#if TARGET_OS_IPHONE
#import <CoreMotion/CoreMotion.h>
#endif
#include <math.h>
#include <stdlib.h>

static GCController *g_held[DM_PLAYERS];   // the controller whose motion handler each player holds, as haptics_apple.m holds its routes
static int g_probe[DM_PLAYERS];            // samples without gravity seen so far; -1 once the controller's IMU verdict is in and logged

static void motion_feed(int player, float ax, float ay, float az, float gx, float gy, float gz, float qx, float qy, float qz, float qw) {
    if (g_probe[player] >= 0) {   // the controller's IMU verdict is not in yet
        if (ax * ax + ay * ay + az * az < 0.25f) {   // no gravity in the sample: an IMU would show about 1 G
            if (++g_probe[player] == 20) { g_probe[player] = -1; LOG("motion: no IMU in player %d's controller, as far as GameController reports", player + 1); }
            return;
        }
        g_probe[player] = -1;
        LOG("motion: player %d's controller IMU feeds six-axis", player + 1);
    }
    float acc[3] = { ax, ay, az }, gyro[3] = { gx, gy, gz }, quat[4] = { -qx, -qy, -qz, qw };
    dm_motion_pad(player, acc, gyro, quat);
}

void dm_motion_apple_controller(int player, GCController *c) {
    if (player < 0 || player >= DM_PLAYERS || g_held[player] == c) return;
    if (g_held[player]) g_held[player].motion.valueChangedHandler = nil;
    g_held[player] = c;
    g_probe[player] = 0;
    if (c) c.motion.valueChangedHandler = ^(GCMotion *m) {
        GCAcceleration g = m.gravity, u = m.userAcceleration; GCRotationRate r = m.rotationRate; GCQuaternion q = m.attitude;
        motion_feed(player, u.x + g.x, u.y + g.y, u.z + g.z, r.x, r.y, r.z, q.x, q.y, q.z, q.w);
    };
}

#if TARGET_OS_IPHONE
// The device's own sensors: Z-vertical reference frame (flat is gravity -Z, as the host's at-rest sensor), arbitrary heading (games recenter yaw themselves)
static CMMotionManager *g_cm;
static BOOL cm_denied;
#endif
void dm_motion_apple_init(void) {
    const char *e = getenv("DM_MOTION"); if (e && !atoi(e)) return;
#if TARGET_OS_IPHONE
    g_cm = [CMMotionManager new];
    if (!g_cm.deviceMotionAvailable) { LOG("motion: this device has no motion sensors; only a controller's IMU would feed six-axis"); return; }
    g_cm.deviceMotionUpdateInterval = 1.0 / 100;
    NSOperationQueue *q = [NSOperationQueue new]; q.maxConcurrentOperationCount = 1;
    [g_cm startDeviceMotionUpdatesUsingReferenceFrame:CMAttitudeReferenceFrameXArbitraryZVertical toQueue:q withHandler:^(CMDeviceMotion *d, NSError *err) {
        if (err) { if (!cm_denied) { cm_denied = YES; LOG("[RED] motion: device motion error: %s", err.localizedDescription.UTF8String ?: "?"); } return; }
        if (!d) return;
        CMQuaternion q = d.attitude.quaternion;
        float acc[3] = { (float)(d.userAcceleration.x + d.gravity.x), (float)(d.userAcceleration.y + d.gravity.y), (float)(d.userAcceleration.z + d.gravity.z) };
        float gyro[3] = { (float)d.rotationRate.x, (float)d.rotationRate.y, (float)d.rotationRate.z };
        float quat[4] = { -(float)q.x, -(float)q.y, -(float)q.z, (float)q.w };
        dm_motion_device(acc, gyro, quat);
    }];
    LOG("motion: the device's own sensors feed six-axis (CoreMotion, 100 Hz)");
#endif
}
