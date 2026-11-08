# POVs app icon

The blue photo cards and ivory heart represent preserving and sharing family memories. The sage rear card connects the icon to the app's optional sage theme. The artwork was created with the built-in image generation tool, then packaged deterministically for Expo and Android.

- `povs-artwork.png`: original generated transparent artwork (1254 x 1254).
- `povs-icon.png`: opaque 1024 x 1024 PNG, square without a baked-in corner mask; used by Expo and suitable as the iOS source icon.
- `povs-adaptive-foreground.png`: transparent 1024 x 1024 foreground with safe padding; background `#F7F9FC`.
- `povs-favicon.png`: opaque 64 x 64 web icon.
- Native Android resources: legacy/round PNGs at 48, 72, 96, 144 and 192px; adaptive foregrounds at 108, 162, 216, 324 and 432px, with API 26+ adaptive XML.

Regenerate the packaged assets from `app/` after installing the locked dependencies:

```sh
node scripts/generate-app-icons.cjs
```

The script uses Expo's existing `jimp-compact` dependency. It only packages the artwork and replaces the named launcher assets; it does not run a full prebuild. The checked-in Android project must carry the native icon resources as well as `app.json`. Changes become visible on an installed Android app after rebuilding and reinstalling.

Sizing references: [Expo app icon guide](https://docs.expo.dev/develop/user-interface/splash-screen-and-app-icon/#app-icon), [Android adaptive icon guide](https://developer.android.com/develop/ui/compose/system/icon_design_adaptive).

## Image generation prompt

Use case: logo-brand. Asset type: production app launcher icon foreground for POVs, a Korean family digital memories and legacy app that stores family photos, stories and shared knowledge. Generate ONE polished emblem on a genuinely transparent background, square 1024 x 1024 PNG. Subject: two overlapping softly rounded photo cards, the front card an elegant muted pastel blue, the rear card a small offset sage-green layer, with one large warm ivory heart centered in the front card. The thick blue frame/card silhouette should suggest protecting cherished family memories. The heart is clear and simple, not tiny, not a speech bubble. Visual style: premium friendly flat matte illustration, precise smooth vector-like edges, generous bold shapes and restrained two-layer depth, no photographic texture. Colors: main muted blue #567FB5, darker blue #355A85 only for subtle definition, restrained sage #A8C6B5 behind, warm ivory #FFF8EE heart. Composition: isolated single centered balanced emblem, approximately 75 percent canvas width and height, all artwork fully inside canvas with ample transparent margins. Strong legibility at 48 pixels. No text, no letters, no wordmark, no watermark, no mockup, no phone, no outer app rounded-square tile, no background color, no checkerboard drawn into the image, no glass, no gloss, no gradients, no tiny decorations, no drop shadow extending into the margins. The output itself is the icon artwork, not a presentation of icons.
