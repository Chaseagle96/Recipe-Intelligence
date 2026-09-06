import Foundation
import SwiftUI

@MainActor
final class IngredientFeedModel: ObservableObject {
    @Published private(set) var recipes: [RemoteRecipe] = []
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var activeIngredient: String?

    private let client: any RecipeIntelligenceClient
    private var generation = UUID()

    init(client: any RecipeIntelligenceClient = LiveRecipeIntelligenceClient()) {
        self.client = client
    }

    func load(vertical: RecipeVertical, ingredient: String, forceRefresh: Bool = true) async {
        let trimmed = ingredient.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            clear()
            return
        }

        generation = UUID()
        let requestGeneration = generation
        activeIngredient = trimmed
        recipes = []
        errorMessage = nil
        isLoading = true
        defer {
            if requestGeneration == generation {
                isLoading = false
            }
        }

        do {
            let manifest = try await client.fetchFeedManifest(
                vertical: vertical,
                forceRefresh: forceRefresh
            )
            guard requestGeneration == generation else { return }

            var matches: [RemoteRecipe] = []
            for pageIndex in manifest.effectiveCorpusPages.indices {
                let page = try await client.fetchCorpusPage(
                    vertical: vertical,
                    pageIndex: pageIndex
                )
                guard requestGeneration == generation else { return }
                matches.append(contentsOf: page.recipes.filter {
                    $0.isExploreEligible && Self.matches($0, ingredient: trimmed)
                })
            }

            guard requestGeneration == generation else { return }
            recipes = Self.rank(Self.unique(matches))
            if recipes.isEmpty {
                errorMessage = "No recipes using \(trimmed) are currently available for \(vertical.name)."
            }
        } catch {
            guard requestGeneration == generation else { return }
            errorMessage = error.localizedDescription
            recipes = []
        }
    }

    func clear() {
        generation = UUID()
        activeIngredient = nil
        recipes = []
        errorMessage = nil
        isLoading = false
    }

    func remove(_ recipe: RemoteRecipe) {
        recipes.removeAll { $0.recipeID == recipe.recipeID && $0.verticalID == recipe.verticalID }
    }

    func restore(_ recipe: RemoteRecipe) {
        guard let activeIngredient,
              Self.matches(recipe, ingredient: activeIngredient),
              recipe.isExploreEligible,
              !recipes.contains(where: { $0.recipeID == recipe.recipeID && $0.verticalID == recipe.verticalID }) else {
            return
        }
        recipes.insert(recipe, at: 0)
    }

    static func matches(_ recipe: RemoteRecipe, ingredient: String) -> Bool {
        let query = normalized(ingredient)
        guard !query.isEmpty else { return true }

        let ingredientText = normalized(recipe.ingredients.joined(separator: " "))
        guard !ingredientText.isEmpty else { return false }

        let terms = query.split(separator: " ").map(String.init)
        return terms.allSatisfy { term in
            ingredientText.range(of: term) != nil
        }
    }

    private static func normalized(_ value: String) -> String {
        value
            .folding(options: [.diacriticInsensitive, .caseInsensitive], locale: .current)
            .lowercased()
            .replacingOccurrences(
                of: "[^a-z0-9]+",
                with: " ",
                options: .regularExpression
            )
            .replacingOccurrences(
                of: "\\s+",
                with: " ",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func unique(_ recipes: [RemoteRecipe]) -> [RemoteRecipe] {
        var seen = Set<String>()
        return recipes.filter {
            seen.insert("\($0.verticalID)|\($0.recipeID)").inserted
        }
    }

    private static func rank(_ recipes: [RemoteRecipe]) -> [RemoteRecipe] {
        recipes.sorted { lhs, rhs in
            if lhs.isGloballyRanked != rhs.isGloballyRanked {
                return lhs.isGloballyRanked && !rhs.isGloballyRanked
            }
            if lhs.isGloballyRanked,
               rhs.isGloballyRanked,
               lhs.rank != rhs.rank {
                return lhs.rank < rhs.rank
            }
            if lhs.hierarchicalScore != rhs.hierarchicalScore {
                return lhs.hierarchicalScore > rhs.hierarchicalScore
            }
            if lhs.evidenceConfidence != rhs.evidenceConfidence {
                return lhs.evidenceConfidence > rhs.evidenceConfidence
            }
            if lhs.ratingCount != rhs.ratingCount {
                return lhs.ratingCount > rhs.ratingCount
            }
            return lhs.title.localizedCaseInsensitiveCompare(rhs.title) == .orderedAscending
        }
    }
}

struct IngredientPickerSheet: View {
    @Environment(\.dismiss) private var dismiss

    let selectedIngredient: String?
    let onApply: (String) -> Void
    let onClear: () -> Void

    @State private var ingredient = ""

    private let suggestions = [
        "Chicken",
        "Beef",
        "Pork",
        "Salmon",
        "Potatoes",
        "Broccoli"
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    TextField("Chicken, potatoes, broccoli…", text: $ingredient)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                        .submitLabel(.search)
                        .onSubmit { apply() }
                        .accessibilityIdentifier("ingredient.search")
                } header: {
                    Text("Ingredient")
                } footer: {
                    Text("Searches structured ingredient lines across Recipe Intelligence’s complete published corpus for the selected cooking method.")
                }

                Section("Quick choices") {
                    ForEach(suggestions, id: \.self) { suggestion in
                        Button(suggestion) {
                            ingredient = suggestion
                            apply()
                        }
                        .accessibilityIdentifier("ingredient.suggestion.\(suggestion.lowercased())")
                    }
                }

                if selectedIngredient != nil {
                    Section {
                        Button("Any Ingredient", role: .destructive) {
                            onClear()
                            dismiss()
                        }
                        .accessibilityIdentifier("ingredient.clear")
                    }
                }
            }
            .navigationTitle("Ingredient")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Show Recipes") { apply() }
                        .disabled(ingredient.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .accessibilityIdentifier("ingredient.apply")
                }
            }
            .onAppear {
                ingredient = selectedIngredient ?? ""
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func apply() {
        let trimmed = ingredient.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        onApply(trimmed)
        dismiss()
    }
}
