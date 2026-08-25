# Recipe Intelligence iOS

Recipe Intelligence for iOS is the personal decision layer on top of the repository's evidence-driven recipe rankings. It targets iOS 17+ with SwiftUI and SwiftData, while the production build and CI use Xcode 26 so current iOS releases receive Apple's newest system appearance and controls. iOS 26+ receives the app's custom Liquid Glass surfaces; older supported releases retain system-material fallbacks.

## Open and run

Open `RecipeIntelligence.xcodeproj` in Xcode 26 or newer. Select the `RecipeIntelligence` scheme and an iPhone simulator. No signing team is required for simulator builds.

The production app reads the vertical catalog from:

`https://raw.githubusercontent.com/Chaseagle96/Recipe-Intelligence/main/api/verticals.json`

For deterministic UI tests, launch with `--ui-testing`; the app uses representative local fixture data and an in-memory SwiftData store.

## Architecture

- `Models.swift`: versioned Recipe Intelligence DTOs and product enums.
- `Networking.swift`: async/await Recipe Intelligence client with vertical discovery, version-aware manifests and paged ranked/corpus feeds.
- `PersistenceModels.swift`: private local SwiftData entities for cache, profiles, households, saves, events, notes, reviews, cooking history, meal plans and shopping items.
- `RecommendationService.swift`: replaceable recommendation interface plus a separate household-convergence interface.
- `ShoppingListService.swift`: ingredient parsing, conservative quantity merging and grocery categorization.
- `AppModel.swift`: main-actor orchestration, behavior-event capture and live feed reconciliation.
- `DesignSystem.swift`: adaptive Liquid Glass surfaces, buttons, grouped glass containers, background treatment and navigation behavior with backwards-compatible fallbacks.
- feature views: Discover, Saved/Elimination, Plan, Shopping, Reviews and Taste/Profile.

Remote Recipe Intelligence evidence is conceptually separate from private user-owned state. The app does not upload notes, reviews or behavioral data.

## iOS 27 and Liquid Glass

The app follows Apple's platform-first Liquid Glass model rather than drawing a custom imitation of the system material everywhere.

- Building with Xcode 26 lets system `TabView`, navigation bars, toolbars, sheets, menus and controls adopt the current platform appearance automatically.
- Custom glass is concentrated on high-value interaction surfaces: vertical filters, recipe ranking/metadata panels, decision controls, detail actions, elimination controls and selected cards.
- Adjacent custom glass controls use `GlassEffectContainer` so rendering and transitions behave as one visual group.
- `.glass` and `.glassProminent` button styles are used on iOS 26+ with ordinary bordered-button fallbacks for iOS 17-25.
- The tab bar minimizes as the user scrolls on supported systems; iOS 27 also minimizes the navigation toolbar on scroll where appropriate.
- The deployment target remains iOS 17.0 so the visual modernization does not unnecessarily drop older devices.

## Mobile backend contract

`api/verticals.json` enumerates available verticals. Each vertical points to its own `docs/api/manifest.json`. One manifest exposes two coordinated views of the same Recipe Intelligence vertical:

1. `pages`: the ordered, deduplicated, evidence-gated current leaderboard used by default Discover. Existing `recipe_count` remains the backwards-compatible ranked count.
2. `corpus_pages`: every normalized recipe record currently retained in that vertical's Recipe Intelligence state, including records outside the current leaderboard.

Pages contain up to 100 recipes and expose factual/derived fields such as recipe and vertical IDs, title, publisher, author, canonical source URL, image URL, ingredient lines, rating evidence, ranking statistics when applicable, and instruction availability/count without republishing publisher instruction prose.

Full-corpus records also carry serving metadata:

- `is_ranked` and `discover_eligible`: whether the record belongs to the authoritative current leaderboard;
- `explore_eligible`: whether the record can safely participate in broader personalized/exploratory retrieval;
- `serveability`: `discover`, `explore`, `archive`, or `suppressed`;
- `status_reasons`: machine-readable reasons such as `stale`, `no_rating_evidence`, `low_evidence`, `evidence_conflict`, `missing_title`, `missing_source_url`, or `duplicate_alias`;
- duplicate representative/group metadata where available.

The iOS client exposes `fetchRecipePage` for the ranked Discover feed and `fetchCorpusPage` for the broader knowledge base. Default Discover intentionally continues using the ranked feed. Future personalized search, household convergence, saved-recipe recovery, and deep exploration can retrieve the wider corpus without weakening Recipe Intelligence's global ranking gate.

The corpus is built from recipes Recipe Intelligence has actually normalized into state. A URL that has merely been discovered, but has never produced a usable normalized recipe record, is counted as catalog coverage rather than being invented as a serveable recipe. `catalog_url_count` in the manifest preserves that distinction.

This is a serving projection only. It does not change Bayesian ranking, priors, calibration, dedupe, crawl state or vertical isolation.

### Zero-rescan backfill

`scripts/backfill_mobile_corpus.py` publishes the full corpus from the existing local state plus the existing leaderboard. It performs no web discovery or recipe crawl and does not modify ranking history. `.github/workflows/mobile-corpus-backfill.yml` runs that projection for Air Fryer and Slow Cooker and commits only their generated `docs/api` serving artifacts. Normal future production refreshes regenerate the corpus automatically from each vertical's current state.

## Live ranking refresh

The iOS binary does not embed a fixed leaderboard. It follows the generated Recipe Intelligence serving artifacts on GitHub.

- App launch force-checks the current vertical catalog and feed manifest.
- Returning to the foreground checks the live catalog and current vertical again.
- While the app remains active, it checks every 15 minutes so an hourly backend refresh can flow into the app without requiring a relaunch.
- Discover supports pull-to-refresh plus an explicit accessible Refresh Rankings button.
- Manifest checks compare `generated_at`; recipe pages are requested with that generation as a version token so stable GitHub raw URLs cannot hide a new snapshot behind HTTP caching.
- If nothing changed, the existing deck is left untouched.
- If rankings changed, the card currently being viewed stays pinned while the unseen pool is replaced and re-ranked from the new snapshot.
- If the user swipes while a refresh is in flight, the finished refresh does not resurrect that card.
- Saved recipe metadata such as rank, rating, rating count, imagery and ingredients is refreshed when the corresponding recipe appears in a newly loaded snapshot. Personal lifecycle state, notes, reviews, cooking history and plans are never overwritten by remote refreshes.
- If the network is unavailable, the app retains the current deck and can fall back to its SwiftData recipe cache.

## Adding a vertical

Add the backend vertical normally, publish its paged mobile feed, then add one entry to `api/verticals.json`. The iOS vertical selector and paged client do not contain Air Fryer/Slow Cooker-specific branching. The live catalog refresh also allows a newly published vertical to appear without shipping a new iOS binary.

## Persistence and learning events

The app records timestamped local events for impressions, opens, save/skip/Not Now swipes, undo, saves, plans, cooking/repeat cooking, favorites, reviews, notes, shopping-list generation, elimination rounds and source opens. Events are profile-scoped and retain the recipe and vertical IDs needed by a future recommendation service.

Multiple user profiles and a household entity exist from day one. Household recommendation is intentionally not faked: the app defines a separate convergence interface, while real per-person predicted enjoyment and household confidence are a future model milestone.

## Accessibility

Every swipe action has an explicit button equivalent. Recipe cards expose VoiceOver labels and custom actions, layouts use semantic Dynamic Type fonts, system colors and large controls, and swipe animations respect Reduce Motion. Pull-to-refresh also has an explicit toolbar button so feed refresh is never gesture-only. The Liquid Glass implementation relies on system materials and controls so platform accessibility appearance adjustments remain authoritative, while pre-iOS-26 systems receive opaque system-surface fallbacks instead of unsupported effects.

## Known limitations

- No account/cloud sync; personal data is local only.
- No public/social reviews or copied publisher comments.
- Publisher instruction prose is not republished; cooking directions open the canonical source page.
- Recommendation logic is a transparent quality/evidence/diversity baseline, not a trained Spotify-level model yet.
- Default Discover intentionally uses only the current ranked feed; full-corpus retrieval is an available data capability for the next personalization/search surfaces, not a license to show stale or suppressed records indiscriminately.
- Shopping normalization is intentionally conservative and does not convert incompatible units.
- Meal planning is one-week local planning without Calendar integration.
- Household convergence is architected but not learned yet.
- iOS background execution is not used to poll GitHub while the app is suspended; an immediate foreground check catches publications that occurred while it was away.

## Validation

`.github/workflows/ios.yml` runs on GitHub's macOS 26 runner with Xcode 26, verifies the selected Xcode version, performs a real simulator build, executes unit plus UI tests, builds an unsigned iPhone app, and packages an unsigned IPA artifact. Python CI independently validates the ranked and full-corpus serving projections and ensures Air Fryer/Slow Cooker ranking pipelines remain healthy.
