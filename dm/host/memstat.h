// Start optional diagnostic samplers after loading the front end's environment.
#pragma once
void dm_diagnostics_start(void);   // Repeated calls do not start duplicate writers.
