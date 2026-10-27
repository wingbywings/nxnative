// Shared real six-axis input for the Mac and iOS front ends: a connected controller's GameController motion, and on iOS the device's own CoreMotion sensors, fed into
// hle_hid.c's rings (dm_motion_pad / dm_motion_device). DM_MOTION=0 keeps the whole path off (and asks no Motion & Fitness permission).
#pragma once
#import <GameController/GameController.h>
void dm_motion_apple_init(void);
void dm_motion_apple_controller(int player, GCController *controller);   // called where dm_haptics_apple_controller is; nil clears the player's handler
