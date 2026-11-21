# Samshush Home 0.5.0

Home shows a microphone icon immediately to the left of the clock when the TV reports active capture on a built-in microphone. Idle, unavailable and expired readings leave that space empty. An active reading expires within two seconds. The indicator runs while Home is visible. It covers the panel microphones only. Neither Home nor its companion reads audio samples.

This release includes the Home widget and microphone-status provider 1.0.0 from Samshush commit `c91ce2c11fffc6af014ca80dbfb0655ac0b6d26a`. The pair was tested on a Samsung Frame Pro QN65LS03FWFXZA running Tizen 9. The TV reported idle in three consecutive checks. A simulated active reading exercised the icon in a browser.

## Install on a development TV

Download `Shortcuts.wgt`, `org.samshush.micstatus-1.0.0.tpk` and `SHA256SUMS` from the release. Both packages are unsigned. The widget ships with weather disabled.

Connect the TV in Developer Mode and use a Samsung TV signing profile registered for that TV. Sign both packages with the same author certificate so Home can query the trusted provider. An update requires the author certificate from the previous installation. [Samsung certificate setup](https://developer.samsung.com/smarttv/develop/getting-started/setting-up-sdk/creating-certificates.html)

From the download directory, set `PROFILE` to the saved profile name and `TV_SERIAL` to the serial from `sdb devices`. The following commands verify the downloads, install them and open Home as an app. [Samsung CLI reference](https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/command-line-interface.html)

```sh
shasum -a 256 -c SHA256SUMS
mkdir -p signed
tizen package -t wgt -s "$PROFILE" -o signed -- Shortcuts.wgt
tizen package -t tpk -s "$PROFILE" -o signed -- org.samshush.micstatus-1.0.0.tpk
tizen install -s "$TV_SERIAL" --name org.samshush.micstatus-1.0.0.tpk -- signed
tizen install -s "$TV_SERIAL" --name Shortcuts.wgt -- signed
tizen run -s "$TV_SERIAL" -p Gxqk7W2na5.Shortcuts
```

## Build from source

From the source archive root, create a widget with weather disabled and run its JavaScript tests.

```sh
mkdir -p build
printf '{}\n' > build/weather-config.json
zip -j build/Shortcuts.wgt launcher/config.xml launcher/index.html launcher/*.js \
  launcher/styles.css launcher/icon.png launcher/water-lilies.jpg build/weather-config.json
node --test launcher/tests/*.test.cjs
```

To compile and sign the provider, set `DOTNET` to the .NET 6 SDK executable, `TIZEN_FRAMEWORK` to the directory containing Tizen 9 managed reference assemblies, and `TIZEN_CLI` to the Tizen CLI executable. The build creates a fresh output directory.

```sh
python3 tools/microphone_status/build.py --dotnet "$DOTNET" --framework "$TIZEN_FRAMEWORK" \
  --cli "$TIZEN_CLI" --profile "$PROFILE" --output build/provider
```

See [LICENSE](LICENSE) for the BSD 3-Clause terms and [NOTICE](NOTICE) for artwork and data credits.
