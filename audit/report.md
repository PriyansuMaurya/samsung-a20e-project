# Galaxy A20e update audit

## Device

- Model: SM-A202F (Galaxy A20e); Android 11, build RP1A.200720.012.A202FXXU5CWF2.
- Security patch: 2023-05-01. This patch level is old; this alone does not prove a newer compatible firmware is available for this device and region.
- Verified boot state reported by the device: green. The ADB shell runs as the ordinary shell user; this does not establish whether root software is present.
- Inventory: 314 installed packages for user 0, including one disabled package (`com.sec.android.emergencylauncher`). See `inventory.csv` for package names, versions, installers and paths. Package names and version data are derived from ADB, not an official update catalog.

## Results

| Category | Result |
| --- | --- |
| Updated | None in this audit. |
| Already current | Not established: the device-specific update catalogs have not been queried. |
| Unsupported | Not established: age and Android version alone do not prove individual packages are unsupported. |
| Failed | No updates attempted. |
| Pending verification | All 314 packages and firmware. |

Google Play Store is installed. The Galaxy Store package was previously removed for the primary user, so Galaxy Store-only app updates may be unavailable. The Samsung software update packages `com.wssyncmldm` and `com.sec.android.soagent` remain installed. No removed package was reinstalled and the disabled package was not enabled.

The Google Play Store provides the supported device-specific app update list via profile > Manage apps & device > Updates available. Samsung's Settings > Software update > Download and install checks firmware availability. Do not install firmware without reviewing the impact and obtaining approval: firmware installation modifies system partitions and is outside this audit's current scope. Galaxy Store restoration would also conflict with the instruction not to reinstall removed bloatware unless separately authorized.

Sources: https://support.google.com/googleplay/answer/113412 and https://www.samsung.com/uk/support/mobile-devices/how-do-i-check-for-operating-system-updates-on-my-samsung-galaxy-device/
