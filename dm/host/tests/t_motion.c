// Controller motion fusion (motion_fusion.h): axis maps, units, orientation from the gyro, tilt correction toward gravity, and the SixAxisSensorState fields. No host, no controller
#include "../motion_fusion.h"
#include <stdio.h>
#include <float.h>

static int checks, fails;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s (line %d)\n", msg, __LINE__); } } while (0)
static int near(float a, float b, float eps) { return fabsf(a - b) <= eps; }

static void run(motion_state *s, const motion_map *m, float ax, float ay, float az, float wx, float wy, float wz, float secs, float k) {
    float a[3] = {ax, ay, az}, w[3] = {wx, wy, wz};
    int n = (int)(secs * 250 + 0.5f);
    for (int i = 0; i < n; i++) motion_update(s, m, a, w, 1 / 250.f, k);
}

static void t_gravity_bias(const motion_map *m) {
    const float dps = (float)M_PI / 180;
    // Rounded GameController samples from the player's BotW log (7606506c): the first pose was resting on a desk, the second held inclined.
    // The old 2 dps raw gate never opened for the desk's ~3.78 dps bias, and its 3 dps component cap could not remove the -3.6 dps on y.
    const float poses[][3] = {{-.07f, .12f, -1.14f}, {.49f, .27f, -.91f}};
    const float bias[3] = {1.08f * dps, -3.6f * dps, -.36f * dps};
    for (unsigned p = 0; p < sizeof poses / sizeof poses[0]; p++) {
        motion_state s; motion_reset(&s); const float *a = poses[p];
        run(&s, m, a[0], a[1], a[2], bias[0], bias[1], bias[2], 25, .5f);
        motion_state before = s;
        run(&s, m, a[0], a[1], a[2], bias[0], bias[1], bias[2], 10, .5f);
        float delta = 0;
        for (int i = 0; i < 3; i++) {
            CHECK(fabsf(s.gyro[i]) * 360 < .01f, "logged large bias: corrected velocity settles at either pose");
            CHECK(fabsf(s.angle[i] - before.angle[i]) * 360 < .02f, "logged large bias: angle stops drifting");
            for (int j = 0; j < 3; j++) delta = fmaxf(delta, fabsf(s.R[i][j] - before.R[i][j]));
        }
        CHECK(delta < .001f, "logged large bias: direction stops drifting");
        // A turn starts immediately after calibration: even its first sample must keep the new velocity.
        float turn[3] = {bias[0], bias[1] + 5 * dps, bias[2]};
        motion_update(&s, m, a, turn, .01f, .5f);
        CHECK(fabsf(s.gyro[1] * 360 - 5) < .02f, "large learned bias: first aiming sample keeps its speed");
    }
    // Gravity cannot distinguish a steady heading turn from a bias parallel to gravity: never bootstrap that component merely because acceleration is constant.
    motion_state s; motion_reset(&s);
    run(&s, m, 0, 0, -1, 0, 0, 3.6f * dps, 30, .5f);
    CHECK(fabsf(s.gyro[2] * 360 - 3.6f) < .001f, "steady yaw is not calibrated out by stationary gravity");
    motion_reset(&s);
    run(&s, m, 0, -.6f, -.8f, 0, 3.6f * .6f * dps, 3.6f * .8f * dps, 30, .5f);
    CHECK(fabsf(s.gyro[1] * 360 - 3.6f * .6f) < .001f && fabsf(s.gyro[2] * 360 - 3.6f * .8f) < .001f,
          "steady world yaw remains intact at an inclined pose");
    // Real tilt changes gravity. A slow 5 dps turn over 10 s must not be mistaken for a large bias.
    motion_reset(&s);
    for (int i = 0; i < 1000; i++) {
        float t = (float)i * .01f, a[3] = {0, -sinf(5 * dps * t), -cosf(5 * dps * t)}, w[3] = {5 * dps, 0, 0};
        motion_update(&s, m, a, w, .01f, .5f);
    }
    CHECK(fabsf(s.gyro[0] * 360 - 5) < .01f, "changing gravity preserves deliberate pitch");
    // Learning on the desk must survive hand tremor when the controller is picked up. This is synthetic 5 Hz pitch motion, not a replay of the log's 1 Hz snapshots.
    motion_reset(&s);
    run(&s, m, 0, 0, -1, bias[0], bias[1], bias[2], 25, .5f);
    for (int i = 0; i < 2000; i++) {
        float t = (float)i * .01f, th = .2f * dps * sinf(10 * (float)M_PI * t), speed = 2 * (float)M_PI * dps * cosf(10 * (float)M_PI * t);
        float a[3] = {0, -sinf(th), -cosf(th)}, w[3] = {bias[0] + speed, bias[1], bias[2]};
        motion_update(&s, m, a, w, .01f, .5f);
        if (i == 1999) CHECK(fabsf(s.gyro[0] * 360 - speed / dps) < .1f, "hand tremor is still reported after desk calibration");
    }
    CHECK(fabsf(s.zero.bias[1] - bias[1]) / dps < .01f, "hand tremor keeps the learned y offset");
    gyro_zero before = s.zero;
    float a[3] = {0, 0, -1}, w[3] = {0, 1, 0};
    gyro_zero_sample(&s.zero, w, a, 1);
    CHECK(s.zero.gravity_s == 0 && s.zero.rest_s == 0 && s.zero.bias[1] == before.bias[1], "a sample gap resets evidence but preserves calibration");
}


static int finite_state(const motion_state *s) {
    for (int i = 0; i < 3; i++) {
        if (!isfinite(s->acc[i]) || !isfinite(s->gyro[i]) || !isfinite(s->angle[i])) return 0;
        if (!isfinite(s->zero.bias[i]) || !isfinite(s->zero.lp[i]) || !isfinite(s->zero.gravity_ref[i])) return 0;
        for (int j = 0; j < 3; j++) if (!isfinite(s->R[i][j])) return 0;
    }
    return 1;
}

static int same_rotation(const motion_state *a, const motion_state *b) {
    for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) if (a->R[i][j] != b->R[i][j]) return 0;
    return 1;
}
static int same_state(const motion_state *a, const motion_state *b) {
    if (a->primed != b->primed || !same_rotation(a, b)) return 0;
    if (a->zero_on != b->zero_on || memcmp(&a->zero, &b->zero, sizeof a->zero)) return 0;
    for (int i = 0; i < 3; i++) if (a->acc[i] != b->acc[i] || a->gyro[i] != b->gyro[i] || a->angle[i] != b->angle[i]) return 0;
    return 1;
}

static void invalid_samples(void) {
    motion_map m; motion_map_parse(&m, NULL);
    float bad[] = {NAN, INFINITY, -INFINITY};
    // Reject the whole sample, even before priming; the next valid sample must
    // give exactly the result it would have given without the rejected one.
    for (int primed = 0; primed < 2; primed++) for (int sensor = 0; sensor < 2; sensor++)
    for (int axis = 0; axis < 3; axis++) for (int b = 0; b < 3; b++) {
        float a[3] = {0, 0, -1}, w[3] = {0.1f, -0.2f, 0.3f};
        motion_state s; motion_reset(&s);
        if (primed) motion_update(&s, &m, a, w, 0, 0.5f);
        motion_state before = s, expected = s;
        (sensor ? w : a)[axis] = bad[b];
        motion_update(&s, &m, a, w, 0.004f, 0.5f);
        CHECK(same_state(&s, &before), "non-finite sample leaves every field unchanged");
        a[0] = a[1] = 0; a[2] = -1; w[0] = 0.1f; w[1] = -0.2f; w[2] = 0.3f;
        motion_update(&s, &m, a, w, 0.004f, 0.5f);
        motion_update(&expected, &m, a, w, 0.004f, 0.5f);
        CHECK(finite_state(&s) && same_state(&s, &expected), "valid sample recovers without resetting fusion");
    }
    float a[3] = {0.2f, 0, -0.98f}, w[3] = {0.1f, 0.2f, 0.3f};
    float times[] = {NAN, INFINITY, -INFINITY, -0.004f, 0, 0.250001f};
    for (int i = 0; i < 6; i++) {
        motion_state s; motion_reset(&s); motion_update(&s, &m, a, w, 0, 0.5f);
        motion_state expected = s;
        motion_update(&s, &m, a, w, times[i], 0.5f);
        motion_update(&expected, &m, a, w, 0, 0.5f);
        CHECK(finite_state(&s) && same_state(&s, &expected), "invalid time or gap reports a sample without integration");
    }
    float gains[] = {NAN, INFINITY, -INFINITY, -1};
    for (int i = 0; i < 4; i++) {
        a[0] = 0.2f;
        motion_state s; motion_reset(&s); motion_update(&s, &m, a, w, 0, 0);
        motion_state expected = s;
        a[0] = -0.2f; // Non-zero tilt error makes an infinite gain observable.
        motion_update(&s, &m, a, w, 0.004f, gains[i]);
        motion_update(&expected, &m, a, w, 0.004f, 0);
        CHECK(finite_state(&s) && same_state(&s, &expected), "invalid gain disables tilt correction");
    }
    // Finite inputs can overflow the float norm or accumulated angle too.
    motion_state s; motion_reset(&s); motion_update(&s, &m, a, w, 0, 0);
    motion_state before = s;
    float large[3] = {FLT_MAX, FLT_MAX, FLT_MAX};
    motion_update(&s, &m, a, large, 0.25f, 0);
    CHECK(finite_state(&s) && same_rotation(&s, &before), "overflowing rotation norm does not corrupt orientation");
    s.angle[0] = FLT_MAX;
    motion_update(&s, &m, a, large, 0.25f, 0);
    CHECK(finite_state(&s) && s.angle[0] == FLT_MAX, "overflowing angle does not poison accumulated revolutions");
}

static void mapping_and_precision(void) {
    int perms[6][3] = {{0,1,2},{0,2,1},{1,0,2},{1,2,0},{2,0,1},{2,1,0}};
    for (int p = 0; p < 6; p++) for (int bits = 0; bits < 8; bits++) {
        char spec[] = "+x,+y,+z";
        for (int i = 0; i < 3; i++) { spec[3*i] = bits >> i & 1 ? '-' : '+'; spec[3*i+1] = "xyz"[perms[p][i]]; }
        motion_map m; CHECK(motion_map_parse(&m, spec), "every signed permutation parses");
        float a[3] = {0.2f, -0.3f, -0.9f}, w[3] = {0.1f, 0.2f, -0.3f};
        motion_state s; motion_reset(&s); motion_update(&s, &m, a, w, 0, 0);
        int ok = 1;
        for (int i = 0; i < 3; i++) {
            ok &= near(s.acc[i], m.sign[i] * a[m.src[i]], 1e-6f);
            ok &= near(s.gyro[i], m.sign[i] * w[m.src[i]] * m.det / (2 * (float)M_PI), 1e-6f);
        }
        CHECK(ok && finite_state(&s), "all 48 frame maps retain acceleration and axial gyro conventions");
    }
    motion_map m; motion_map_parse(&m, NULL);
    motion_state s; motion_reset(&s); s.zero_on = 0;   // Test raw integration precision; zero-drift intentionally removes a steady sub-threshold rate.
    run(&s, &m, 0, 0, -1, 0, 0, 0.0001f, 300, 0);
    CHECK(finite_state(&s) && near(s.angle[2], 0.03f / (2 * (float)M_PI), 5e-6f), "small rotations retain precision over five minutes");
    CHECK(near(s.R[0][0], cosf(0.03f), 1e-4f) && near(s.R[1][0], sinf(0.03f), 1e-4f), "small rotations accumulate in orientation");
}

int main(void) {
    motion_map m;
    CHECK(motion_map_parse(&m, NULL) && m.src[0] == 0 && m.det == 1, "default map is the identity");
    CHECK(motion_map_parse(&m, "x,-z,y") && m.src[1] == 2 && m.sign[1] == -1 && m.det == 1, "x,-z,y is a proper rotation");
    CHECK(motion_map_parse(&m, "y,x,z") && m.det == -1, "a swap is a mirror");
    CHECK(motion_map_parse(&m, "-x, y, z") && m.det == -1, "one sign flip is a mirror");
    CHECK(!motion_map_parse(&m, "x,x,z") && m.det == 1, "a repeated axis is refused (identity)");
    CHECK(!motion_map_parse(&m, "x,y") , "two entries are refused");

    // flat and still: identity orientation, acc (0,0,-1), no rotation
    motion_map id; motion_map_parse(&id, NULL);
    t_gravity_bias(&id);
    motion_state s; motion_reset(&s);
    run(&s, &id, 0, 0, -1, 0, 0, 0, 1, 0.5f);
    float acc[3], gyro[3], ang[3], dir[9];
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(acc[2], -1, 1e-5f) && near(dir[0], 1, 1e-4f) && near(dir[4], 1, 1e-4f) && near(dir[8], 1, 1e-4f), "flat: identity direction");

    // a quarter turn of yaw over one second (pi/2 rad/s): gyro 0.25 rev/s, angle 0.25 rev, the controller's x axis now points along world y (as the old shake's convention)
    motion_reset(&s);
    run(&s, &id, 0, 0, -1, 0, 0, (float)M_PI / 2, 1, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(gyro[2], 0.25f, 1e-4f), "gyro in revolutions / s");
    CHECK(near(ang[2], 0.25f, 0.01f), "angle integrates to a quarter revolution");
    CHECK(near(dir[0], 0, 0.02f) && near(dir[1], 1, 0.02f), "x axis turned to world y");

    // pitch: the top raised 30 degrees about x, with gravity measured to match: stays there (correction agrees with the gyro)
    motion_reset(&s);
    float th = 30 * (float)M_PI / 180;
    run(&s, &id, 0, 0, -1, 0, 0, 0, 0.1f, 0.5f);
    run(&s, &id, 0, 0, -1, th, 0, 0, 1, 0);          // turn by the gyro alone (no correction while the accelerometer lags)
    run(&s, &id, 0, -sinf(th), -cosf(th), 0, 0, 0, 2, 0.5f);   // then still, gravity seen tilted in the body
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(dir[5], sinf(th), 0.02f) && near(dir[4], cosf(th), 0.02f), "y axis raised 30 degrees");

    // tilt drift: the gyro says nothing while gravity shows a 20-degree roll: the correction pulls the orientation there
    motion_reset(&s);
    run(&s, &id, 0, 0, -1, 0, 0, 0, 0.1f, 0.5f);
    float r = 20 * (float)M_PI / 180;
    run(&s, &id, sinf(r), 0, -cosf(r), 0, 0, 0, 20, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(-dir[2], sinf(r), 0.03f), "roll pulled toward gravity");

    // a source that starts tilted is primed level with its gravity
    motion_reset(&s);
    run(&s, &id, sinf(r), 0, -cosf(r), 0, 0, 0, 0.01f, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(-dir[2], sinf(r), 0.02f), "primed from the first sample's gravity");

    // A phone held upright starts with gravity on -y, parallel to the old heading reference.
    // All six axis-aligned poses must map their measured gravity to world down on the first sample.
    for (int axis = 0; axis < 3; axis++) for (int sign = -1; sign <= 1; sign += 2) {
        float a[3] = {0}, w[3] = {0}; a[axis] = (float)sign;
        motion_reset(&s); motion_update(&s, &id, a, w, 0, 0.5f);
        CHECK(near(s.R[2][axis] * sign, -1, 1e-5f), "first sample: gravity points down in every starting pose");
    }

    // a frame map: Apple reports rotation about its y where the Switch has z (map "x,-z,y": switch y = -apple z, switch z = apple y)
    motion_map fm; motion_map_parse(&fm, "x,-z,y");
    motion_reset(&s);
    run(&s, &fm, 0, -1, 0, 0, (float)M_PI / 2, 0, 1, 0.5f);   // Apple: gravity along -y, rotating about +y
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(acc[2], -1, 1e-4f) && near(gyro[2], 0.25f, 1e-4f), "mapped: gravity on -z, yaw on z");
    CHECK(near(dir[1], 1, 0.02f), "mapped yaw turns x to world y");

    // the specific-force convention: +1 G up at rest becomes gravity down with DM_MOTION_ACC_SIGN=-1
    motion_map fs; motion_map_parse(&fs, NULL); fs.acc_sign = -1;
    motion_reset(&s);
    run(&s, &fs, 0, 0, 1, 0, 0, 0, 0.2f, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(acc[2], -1, 1e-5f) && near(dir[8], 1, 1e-4f), "acc sign flip");

    // orthonormal after a long noisy spin
    motion_reset(&s);
    for (int i = 0; i < 25000; i++) { float a[3] = {0, 0, -1}, w[3] = {0.7f * sinf(i * 0.01f), 1.3f, -0.4f}; motion_update(&s, &id, a, w, 1 / 250.f, 0.5f); }
    float d01 = s.R[0][0] * s.R[0][1] + s.R[1][0] * s.R[1][1] + s.R[2][0] * s.R[2][1], n0 = s.R[0][0] * s.R[0][0] + s.R[1][0] * s.R[1][0] + s.R[2][0] * s.R[2][0];
    CHECK(near(d01, 0, 1e-4f) && near(n0, 1, 1e-4f), "orientation stays orthonormal");

    // zero drift (gyro_zero): a controller at rest whose gyro carries a yaw bias holds still -- the learned zero point takes the bias out of the reported velocity, the
    // integrated angle and the direction, so a still controller's aim creeps nowhere (TotK's gyro aiming crept to one side until this)
    float dps = (float)M_PI / 180;
    motion_reset(&s);
    run(&s, &id, 0, 0, -1, 0, 0, 0.5f * dps, 20, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(fabsf(gyro[2]) < 0.02f * dps / (2 * (float)M_PI), "a resting bias is learned away");
    float dir1 = dir[1], ang1 = ang[2];
    run(&s, &id, 0, 0, -1, 0, 0, 0.5f * dps, 5, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(fabsf(ang[2] - ang1) < 1e-4f && fabsf(dir[1] - dir1) < 1e-3f, "the angle and the direction stop creeping");
    // iPhone + Bluetooth Pro: aiming reportedly holds still only while flat. Exercise bias on every body axis, including y (which becomes world yaw when held upright),
    // at pitched, rolled and compound poses. Learning and a settled orientation must not depend on lying face up.
    const float poses[][3] = {{0, 0, -1}, {0, -0.70710678f, -0.70710678f}, {0, -1, 0},
                             {0.70710678f, 0, -0.70710678f}, {0.5f, -0.5f, -0.70710678f}, {0, 0, 1}};
    for (unsigned p = 0; p < sizeof poses / sizeof poses[0]; p++) for (int axis = 0; axis < 3; axis++) {
        motion_state tilted; motion_reset(&tilted);
        const float *a = poses[p]; float w[3] = {0}; w[axis] = 0.5f * dps;
        run(&tilted, &id, a[0], a[1], a[2], w[0], w[1], w[2], 20, 0.5f);
        motion_state before = tilted;
        run(&tilted, &id, a[0], a[1], a[2], w[0], w[1], w[2], 10, 0.5f);
        float delta = 0;
        for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) delta = fmaxf(delta, fabsf(tilted.R[i][j] - before.R[i][j]));
        CHECK(fabsf(tilted.gyro[axis]) * 360 < 0.001f, "every pose: body-axis bias is learned away");
        CHECK(fabsf(tilted.angle[axis] - before.angle[axis]) * 360 < 0.01f, "every pose: settled angle stays still");
        CHECK(delta < 0.0002f, "every pose: settled direction stays still");
        float gravity_dot = 0;
        for (int i = 0; i < 3; i++) gravity_dot -= tilted.R[2][i] * a[i];
        CHECK(near(gravity_dot, 1, 1e-5f), "every pose: direction agrees with measured gravity");
    }
    // a turn after the bias was learned reads whole minus the bias, and learning never runs while it turns
    run(&s, &id, 0, 0, -1, 0, 0, 30 * dps, 1, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(fabsf(gyro[2] * 2 * (float)M_PI - 29.5f * dps) < 0.1f * dps, "a turn after learning keeps its whole speed less the bias");
    motion_reset(&s);
    run(&s, &id, 0, 0, -1, 0, 0, 10 * dps, 10, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(fabsf(gyro[2] * 2 * (float)M_PI - 10 * dps) < 0.02f * dps, "no zero is learned from a steady turn");
    CHECK(fabsf(ang[2] - 10 * dps * 10 / (2 * (float)M_PI)) < 1e-3f, "angle integrates the steady turn");
    // the switch off: the raw bias stays (DM_MOTION_ZERO=0)
    motion_reset(&s); s.zero_on = 0;
    run(&s, &id, 0, 0, -1, 0, 0, 0.5f * dps, 20, 0.5f);
    motion_fill(&s, acc, gyro, ang, dir);
    CHECK(near(gyro[2] * 2 * (float)M_PI, 0.5f * dps, 1e-5f), "the raw bias stays with zero-drift off");

    // the phone's own sensors: held upright in landscape, screen toward the player, gravity is toward the bottom of the picture (Switch -y) whichever way it is turned
    float up3[3] = {-1, 0, 0}, up4[3] = {1, 0, 0}, flat[3] = {0, 0, -1}, o[3];
    motion_phone_to_switch(3, up3, o); CHECK(near(o[0], 0, 1e-6f) && near(o[1], -1, 1e-6f) && near(o[2], 0, 1e-6f), "landscape right upright: gravity to the picture's bottom");
    motion_phone_to_switch(4, up4, o); CHECK(near(o[0], 0, 1e-6f) && near(o[1], -1, 1e-6f), "landscape left upright: gravity to the picture's bottom");
    motion_phone_to_switch(3, flat, o); CHECK(near(o[2], -1, 1e-6f), "flat face up stays face up");
    // turning the phone (landscape right) about its own long side, which is the picture's x axis: rotation about phone -y becomes rotation about Switch +x
    float rl[3] = {0, -1, 0}; motion_phone_to_switch(3, rl, o); CHECK(near(o[0], 1, 1e-6f) && near(o[1], 0, 1e-6f), "rotation axes follow the picture");

    invalid_samples();
    mapping_and_precision();

    printf("t_motion: %d checks, %d failed\n", checks, fails);
    return fails != 0;
}
