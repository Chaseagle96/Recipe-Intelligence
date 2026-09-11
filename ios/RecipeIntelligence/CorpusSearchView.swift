import SwiftUI

struct CorpusSearchView: View {
    @EnvironmentObject private var appModel: AppModel
    @StateObject private var searchModel = CorpusSearchModel()
    @State private var selectedRecipe: RemoteRecipe?

    var body: some View {
        Group {
            if searchModel.isLoading && searchModel.corpusCount == 0 {
                ProgressView("Loading Recipe Intelligence corpus…")
                    .controlSize(.large)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if searchModel.query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                ContentUnavailableView {
                    Label("Search every recipe", systemImage: "magnifyingglass")
                } description: {
                    if searchModel.corpusCount > 0 {
                        Text("Search \(searchModel.corpusCount.formatted()) recipes across every Recipe Intelligence method by title, ingredient, category, source, or cooking method.")
                    } else {
                        Text(searchModel.errorMessage ?? "Search the complete Recipe Intelligence recipe corpus.")
                    }
                }
            } else if searchModel.results.isEmpty {
                ContentUnavailableView.search(text: searchModel.query)
            } else {
                List(searchModel.results) { recipe in
                    Button {
                        appModel.recordOpened(recipe)
                        selectedRecipe = recipe
                    } label: {
                        SearchRecipeRow(recipe: recipe)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("search.result.\(recipe.recipeID)")
                }
                .listStyle(.plain)
                .refreshable {
                    await searchModel.reload()
                }
            }
        }
        .recipeScreenBackground()
        .navigationTitle("Search")
        .navigationBarTitleDisplayMode(.inline)
        .searchable(
            text: $searchModel.query,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Recipes, ingredients, methods…"
        )
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .task {
            await searchModel.loadIfNeeded()
        }
        .sheet(item: $selectedRecipe) { recipe in
            NavigationStack {
                RemoteRecipeDetailView(recipe: recipe)
            }
        }
        .safeAreaInset(edge: .bottom) {
            if !searchModel.query.isEmpty && !searchModel.results.isEmpty {
                Text("\(searchModel.results.count.formatted()) result\(searchModel.results.count == 1 ? "" : "s") · \(searchModel.corpusCount.formatted()) recipes indexed")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                    .recipeGlassSurface(cornerRadius: 14)
                    .padding(.bottom, 4)
            }
        }
        .accessibilityIdentifier("search.screen")
    }
}

private struct SearchRecipeRow: View {
    let recipe: RemoteRecipe

    var body: some View {
        HStack(spacing: 12) {
            RemoteRecipeImage(url: recipe.photoURL, title: recipe.title)
                .frame(width: 82, height: 82)
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                .clipped()

            VStack(alignment: .leading, spacing: 5) {
                Text(recipe.title)
                    .font(.headline)
                    .foregroundStyle(.primary)
                    .lineLimit(2)

                HStack(spacing: 6) {
                    Label(recipe.verticalName, systemImage: methodIcon)
                    Text("·")
                    Text("★ \(String(format: "%.1f", recipe.rating))")
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)

                Text(recipe.source)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)

                if !recipe.ingredients.isEmpty {
                    Text(recipe.ingredients.prefix(2).joined(separator: " · "))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 0)

            Image(systemName: "chevron.right")
                .font(.caption.bold())
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
    }

    private var methodIcon: String {
        switch recipe.verticalID {
        case "air_fryer": return "wind"
        case "slow_cooker": return "clock.arrow.circlepath"
        default: return "fork.knife"
        }
    }
}

@MainActor
final class CorpusSearchModel: ObservableObject {
    @Published var query = "" {
        didSet { updateResults() }
    }
    @Published private(set) var results: [RemoteRecipe] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var corpusCount = 0

    private let client: any RecipeIntelligenceClient
    private var corpus: [RemoteRecipe] = []
    private var generation = UUID()

    init() {
        let isUITesting = ProcessInfo.processInfo.arguments.contains("--ui-testing")
        self.client = isUITesting ? PreviewRecipeIntelligenceClient() : LiveRecipeIntelligenceClient()
    }

    func loadIfNeeded() async {
        guard corpus.isEmpty, !isLoading else { return }
        await load(forceRefresh: false)
    }

    func reload() async {
        await load(forceRefresh: true)
    }

    private func load(forceRefresh: Bool) async {
        generation = UUID()
        let requestGeneration = generation
        isLoading = true
        errorMessage = nil
        defer {
            if requestGeneration == generation {
                isLoading = false
            }
        }

        do {
            let verticals = try await client.fetchVerticals(forceRefresh: forceRefresh)
            guard requestGeneration == generation else { return }

            var loaded: [RemoteRecipe] = []
            for vertical in verticals where vertical.available {
                let manifest = try await client.fetchFeedManifest(
                    vertical: vertical,
                    forceRefresh: forceRefresh
                )
                guard requestGeneration == generation else { return }

                for pageIndex in manifest.effectiveCorpusPages.indices {
                    let page = try await client.fetchCorpusPage(
                        vertical: vertical,
                        pageIndex: pageIndex
                    )
                    guard requestGeneration == generation else { return }
                    loaded.append(contentsOf: page.recipes.filter(\.isExploreEligible))
                }
            }

            guard requestGeneration == generation else { return }
            corpus = Self.unique(loaded)
            corpusCount = corpus.count
            updateResults()
        } catch {
            guard requestGeneration == generation else { return }
            errorMessage = error.localizedDescription
            if corpus.isEmpty {
                results = []
                corpusCount = 0
            }
        }
    }

    private func updateResults() {
        let normalizedQuery = Self.normalized(query)
        guard !normalizedQuery.isEmpty else {
            results = []
            return
        }

        let terms = normalizedQuery.split(separator: " ").map(String.init)
        let matches = corpus.compactMap { recipe -> (RemoteRecipe, Int)? in
            let title = Self.normalized(recipe.title)
            let ingredients = Self.normalized(recipe.ingredients.joined(separator: " "))
            let categories = Self.normalized(recipe.categories.joined(separator: " "))
            let source = Self.normalized(recipe.source)
            let author = Self.normalized(recipe.author)
            let method = Self.normalized(recipe.verticalName)
            let haystack = [title, ingredients, categories, source, author, method].joined(separator: " ")

            guard terms.allSatisfy({ haystack.contains($0) }) else { return nil }

            var score = 0
            if title == normalizedQuery { score += 120 }
            if title.hasPrefix(normalizedQuery) { score += 80 }
            if title.contains(normalizedQuery) { score += 55 }
            score += terms.filter { title.contains($0) }.count * 18
            score += terms.filter { ingredients.contains($0) }.count * 12
            score += terms.filter { categories.contains($0) }.count * 8
            score += terms.filter { method.contains($0) }.count * 8
            score += terms.filter { source.contains($0) }.count * 4
            if recipe.isGloballyRanked { score += 8 }
            score += min(8, Int(recipe.evidenceConfidence * 8))
            return (recipe, score)
        }

        results = matches
            .sorted { lhs, rhs in
                if lhs.1 != rhs.1 { return lhs.1 > rhs.1 }
                if lhs.0.isGloballyRanked != rhs.0.isGloballyRanked {
                    return lhs.0.isGloballyRanked && !rhs.0.isGloballyRanked
                }
                if lhs.0.isGloballyRanked,
                   rhs.0.isGloballyRanked,
                   lhs.0.rank != rhs.0.rank {
                    return lhs.0.rank < rhs.0.rank
                }
                if lhs.0.hierarchicalScore != rhs.0.hierarchicalScore {
                    return lhs.0.hierarchicalScore > rhs.0.hierarchicalScore
                }
                return lhs.0.title.localizedCaseInsensitiveCompare(rhs.0.title) == .orderedAscending
            }
            .map(\.0)
    }

    private static func normalized(_ value: String) -> String {
        value
            .folding(options: [.diacriticInsensitive, .caseInsensitive], locale: .current)
            .lowercased()
            .replacingOccurrences(of: "[^a-z0-9]+", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func unique(_ recipes: [RemoteRecipe]) -> [RemoteRecipe] {
        var seen = Set<String>()
        return recipes.filter {
            seen.insert("\($0.verticalID)|\($0.recipeID)").inserted
        }
    }
}
