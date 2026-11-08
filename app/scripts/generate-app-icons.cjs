// Package the generated artwork using Expo's existing image tooling dependency.
// Run from app/: node scripts/generate-app-icons.cjs
const fs = require("node:fs/promises");
const path = require("node:path");
const Jimp = require("jimp-compact");
const ROOT = path.resolve(__dirname, "..");
const ASSETS = path.join(ROOT, "assets/branding");
const RES = path.join(ROOT, "android/app/src/main/res");
const BACKGROUND = "#F7F9FC";
const DENSITIES = { mdpi: 1, hdpi: 1.5, xhdpi: 2, xxhdpi: 3, xxxhdpi: 4 };

async function save(image, file, opaque = false) {
  await fs.mkdir(path.dirname(file), { recursive: true });
  if (opaque) image.colorType(2);
  await fs.writeFile(file, await image.getBufferAsync(Jimp.MIME_PNG));
}

function place(mark, extent, background) {
  const scaled = mark.clone().scaleToFit(extent, extent, Jimp.RESIZE_BICUBIC);
  return new Jimp(1024, 1024, background).composite(scaled,
    Math.round((1024 - scaled.bitmap.width) / 2),
    Math.round((1024 - scaled.bitmap.height) / 2));
}

async function main() {
  const source = await Jimp.read(path.join(ASSETS, "povs-artwork.png"));
  let left = source.bitmap.width, top = source.bitmap.height, right = -1, bottom = -1;
  // Ignore almost transparent fringe when measuring the design's visual bounds.
  source.scan(0, 0, source.bitmap.width, source.bitmap.height, function(x, y, index) {
    if (this.bitmap.data[index + 3] <= 16) return;
    left = Math.min(left, x); top = Math.min(top, y);
    right = Math.max(right, x); bottom = Math.max(bottom, y);
  });
  if (right < left) throw new Error("Icon artwork is empty");
  const mark = source.clone().crop(left, top, right - left + 1, bottom - top + 1);
  const icon = place(mark, 760, BACKGROUND);
  await save(icon, path.join(ASSETS, "povs-icon.png"), true);
  await save(icon.clone().resize(64, 64, Jimp.RESIZE_BICUBIC), path.join(ASSETS, "povs-favicon.png"), true);

  // Fit meaningful pixels within a central 64dp diameter, inside Android's
  // 66dp safe area on its 108dp adaptive canvas. Preserve the original colors.
  let radius = 0;
  const cx = (mark.bitmap.width - 1) / 2, cy = (mark.bitmap.height - 1) / 2;
  mark.scan(0, 0, mark.bitmap.width, mark.bitmap.height, function(x, y, index) {
    if (this.bitmap.data[index + 3] > 16) radius = Math.max(radius, Math.hypot(x - cx, y - cy));
  });
  const extent = Math.floor(Math.max(mark.bitmap.width, mark.bitmap.height) * (1024 * 32 / 108) / radius);
  const foreground = place(mark, extent, 0x00000000);
  await save(foreground, path.join(ASSETS, "povs-adaptive-foreground.png"));

  for (const [density, scale] of Object.entries(DENSITIES)) {
    const dir = path.join(RES, `mipmap-${density}`), size = Math.round(48 * scale);
    await save(icon.clone().resize(size, size, Jimp.RESIZE_BICUBIC), path.join(dir, "ic_launcher.png"), true);
    const round = icon.clone().circle().resize(size, size, Jimp.RESIZE_BICUBIC);
    await save(round, path.join(dir, "ic_launcher_round.png"));
    await save(foreground.clone().resize(Math.round(108 * scale), Math.round(108 * scale), Jimp.RESIZE_BICUBIC), path.join(dir, "ic_launcher_foreground.png"));
    // Remove only the two replaced files, avoiding duplicate Android resource IDs.
    for (const name of ["ic_launcher.webp", "ic_launcher_round.webp"]) {
      await fs.rm(path.join(dir, name), { force: true });
    }
  }

  const adaptiveDir = path.join(RES, "mipmap-anydpi-v26");
  await fs.mkdir(adaptiveDir, { recursive: true });
  const adaptiveXml = '<?xml version="1.0" encoding="utf-8"?>\n<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n  <background android:drawable="@drawable/povs_icon_background" />\n  <foreground android:drawable="@mipmap/ic_launcher_foreground" />\n</adaptive-icon>\n';
  for (const name of ["ic_launcher.xml", "ic_launcher_round.xml"]) {
    await fs.writeFile(path.join(adaptiveDir, name), adaptiveXml);
  }
  await fs.writeFile(path.join(RES, "drawable/povs_icon_background.xml"),
    `<?xml version="1.0" encoding="utf-8"?>\n<shape xmlns:android="http://schemas.android.com/apk/res/android" android:shape="rectangle">\n  <solid android:color="${BACKGROUND}" />\n</shape>\n`);
  console.log("Generated 1024px app/adaptive icons, 64px favicon, and Android launcher resources for five densities.");
}

main().catch(error => { console.error(error.message); process.exitCode = 1; });
