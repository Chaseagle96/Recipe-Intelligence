import SwiftUI

struct RemoteRecipeDetailView: View {
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    let recipe: RemoteRecipe

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                RemoteRecipeImage(url: recipe.photoURL, title: recipe.title)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(1.18, contentMode: .fill)
                    .clipped()
                    .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
                    .overlay(alignment: .bottomLeading) {
                        Text("#\(recipe.rank) · \(recipe.verticalName)")
                            .font(.subheadline.weight(.bold))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 9)
                            .recipeGlassSurface(cornerRadius: 18)
                            .padding(14)
                    }

                VStack(alignment: .leading, spacing: 12) {
                    Text(recipe.title)
                        .font(.largeTitle.bold())
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    Text("From \(recipe.source)")
                        .font(.headline)
                        .fixedSize(horizontal: false, vertical: true)
                    if !recipe.author.isEmpty {
                        Text("By \(recipe.author)")
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    metricCluster
                }
                .padding(18)
                .frame(maxWidth: .infinity, alignment: .leading)
                .recipeGlassSurface(cornerRadius: RecipeDesign.cornerRadius)

                if !recipe.ingredients.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        sectionTitle("Ingredients")
                        ForEach(Array(recipe.ingredients.enumerated()), id: \.offset) { _, ingredient in
                            Label(ingredient, systemImage: "circle.fill")
                                .symbolRenderingMode(.hierarchical)
                                .font(.body)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                VStack(alignment: .leading, spacing: 8) {
                    sectionTitle("Cooking directions")
                    if recipe.hasInstructions {
                        Text("Recipe Intelligence found structured directions, but the app does not republish publisher instruction prose. Open the original recipe for the complete cooking method.")
                    } else {
                        Text("Open the original publisher page for the complete cooking method.")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .foregroundStyle(.secondary)

                if !recipe.rankProvenance.isEmpty {
                    DisclosureGroup("Why this ranks here") {
                        Text(recipe.rankProvenance)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .padding(.top, 6)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .recipeGlassSurface(cornerRadius: RecipeDesign.compactCornerRadius)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal)
            .padding(.top, 8)
            .padding(.bottom, 110)
        }
        .recipeScreenBackground()
        .safeAreaInset(edge: .bottom) {
            RecipeGlassGroup(spacing: 10) {
                HStack(spacing: 10) {
                    Button("Save to Try", systemImage: "heart.fill") {
                        appModel.saveFromDetail(recipe)
                    }
                    .recipeGlassButton(prominent: true)
                    .accessibilityIdentifier("detail.save")

                    if let url = recipe.sourceURL {
                        Button("Original", systemImage: "arrow.up.right.square") {
                            appModel.recordOriginalSourceOpened(recipeID: recipe.recipeID, verticalID: recipe.verticalID)
                            openURL(url)
                        }
                        .recipeGlassButton()
                    }
                }
                .frame(maxWidth: .infinity)
                .padding(.horizontal)
                .padding(.vertical, 8)
            }
        }
        .navigationTitle("Recipe")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button("Close", systemImage: "xmark") { dismiss() }
                    .labelStyle(.iconOnly)
                    .accessibilityIdentifier("detail.close")
            }
        }
        .recipeToolbarBehavior()
    }

    private var metricCluster: some View {
        RecipeGlassGroup(spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 10) {
                    ratingPill
                    ratingCountPill
                    confidencePill
                }

                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        ratingPill
                        ratingCountPill
                    }
                    confidencePill
                }

                VStack(alignment: .leading, spacing: 8) {
                    ratingPill
                    ratingCountPill
                    confidencePill
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var ratingPill: some View {
        RecipeMetricPill(title: String(format: "%.1f", recipe.rating), systemImage: "star.fill")
    }

    private var ratingCountPill: some View {
        RecipeMetricPill(title: "\(recipe.ratingCount.formatted()) ratings", systemImage: "person.2.fill")
    }

    private var confidencePill: some View {
        RecipeMetricPill(title: recipe.confidenceLabel, systemImage: "checkmark.seal.fill")
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title)
            .font(.title2.bold())
            .accessibilityAddTraits(.isHeader)
    }
}
