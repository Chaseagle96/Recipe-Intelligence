import SwiftUI

struct RootView: View {
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject private var appModel: AppModel

    var body: some View {
        TabView {
            NavigationStack { DiscoverView() }
                .tabItem { Label("Discover", systemImage: "sparkles") }
                .accessibilityIdentifier("tab.discover")

            NavigationStack { SavedView() }
                .tabItem { Label("Saved", systemImage: "heart.fill") }
                .accessibilityIdentifier("tab.saved")

            NavigationStack { CorpusSearchView() }
                .tabItem { Label("Search", systemImage: "magnifyingglass") }
                .accessibilityIdentifier("tab.search")

            NavigationStack { PlannerView() }
                .tabItem { Label("Plan", systemImage: "calendar") }
                .accessibilityIdentifier("tab.plan")

            NavigationStack { ShoppingView() }
                .tabItem { Label("Shopping", systemImage: "cart") }
                .accessibilityIdentifier("tab.shopping")
        }
        .recipeTabBarBehavior()
        .task { await appModel.bootstrap() }
        .onChange(of: scenePhase) { _, newPhase in
            guard newPhase == .active else { return }
            Task { await appModel.refreshCurrentFeed(trigger: .foreground) }
        }
        .task(id: scenePhase) {
            guard scenePhase == .active else { return }
            do {
                while !Task.isCancelled {
                    try await Task.sleep(for: .seconds(15 * 60))
                    guard !Task.isCancelled else { return }
                    await appModel.refreshCurrentFeed(trigger: .periodic)
                }
            } catch {
                // Scene transitions cancel this task. The next active scene starts
                // a fresh timer and performs an immediate foreground refresh.
            }
        }
    }
}
