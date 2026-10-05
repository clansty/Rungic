# rungic-firefox
install -Dm755 "$SRC/desktop/firefox" "$DESTDIR/usr/bin/firefox"
install -Dm755 "$SRC/desktop/firefox-workspace-profile" "$DESTDIR/usr/libexec/rungic-firefox-workspace-profile"
# Install the same runtime subset as before, directly from the patched upstream source.
# Its Makefile also adds metainfo and policies; Rungic maintains its own policies below.
task_mcf="$SRC/upstream/mobile-config-firefox/src"
mkdir -p "$DESTDIR/usr/lib/mobile-config-firefox"
cp -a "$task_mcf/modules/." "$DESTDIR/usr/lib/mobile-config-firefox/"
cp -a "$task_mcf/themes" "$DESTDIR/usr/lib/mobile-config-firefox/"
install -Dm644 "$task_mcf/mobile-config-prefs.js" "$DESTDIR/usr/lib/firefox/defaults/pref/mobile-config-prefs.js"
install -Dm644 "$task_mcf/mobile-config-autoconfig.js" "$DESTDIR/usr/lib/firefox/mobile-config-autoconfig.js"
install -Dm644 "$SRC/desktop/firefox-policies.json" "$DESTDIR/usr/lib/firefox/distribution/policies.json"
install -Dm644 "$SRC/desktop/firefox-codec-prefs.js" "$DESTDIR/usr/lib/firefox/defaults/pref/rungic-codec.js"
# Firefox loads FFmpeg from its own directory first: the private build with the moto codecs.
for lib in libavcodec.so.62 libavutil.so.60 libswresample.so.6 libswscale.so.9; do
    ln -s "/usr/lib/rungic-codec/ffmpeg/lib/$lib" "$DESTDIR/usr/lib/firefox/$lib"
done
