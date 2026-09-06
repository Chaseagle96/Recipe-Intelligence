import SwiftData
import SwiftUI

private enum SavedSortOption: String, CaseIterable, Identifiable, Hashable {
    case recentlySaved = "Recently Saved"
    case recipeRank = "Recipe Intelligence Rank"
    case personalRating = "My Rating"

    var id: String { rawValue }
}

struct SavedView: View {
    @EnvironmentObject private var appModel: AppModel
    @Query(sort: \SavedRecipeRecord.savedAt, order: .reverse) private var allSaved: [SavedRecipeRecord]
    @Query(sort: \PersonalReviewRecord.createdAt, order: .reverse) private var allReviews: [PersonalReviewRecord]
    @State private var searchText = ""
    @State private var statusFilter = "All"
    @State private var verticalFilter = "All"
    @State private var sortOption: SavedSortOption = .recentlySaved
    @State private var showPicker = false

    private var profileSaved: [SavedRecipeRecord] {
        allSaved.filter { $0.profileID == appModel.activeProfileID }
    }

    private var saved: [SavedRecipeRecord] {
        let filtered = profileSaved.filter { record in
            let matchesSearch = searchText.isEmpty || record.title.localizedCaseInsensitiveContains(searchText) || record.source.localizedCaseInsensitiveContains(searchText)
            let matchesStatus = statusFilter == "All" || record.status.rawValue == statusFilter
            let matchesVertical = verticalFilter == "All" || record.verticalName == verticalFilter
            return matchesSearch && matchesStatus && matchesVertical
        }

        return filtered.sorted { lhs, rhs in
            switch sortOption {
            case .recentlySaved:
                return lhs.savedAt > rhs.savedAt
            case .recipeRank:
                let lhsRank = lhs.rank > 0 ? lhs.rank : Int.max
                let rhsRank = rhs.rank > 0 ? rhs.rank : Int.max
                if lhsRank != rhsRank { return lhsRank < rhsRank }
                return lhs.savedAt > rhs.savedAt
            case .personalRating:
                let lhsRating = personalRating(for: lhs)
                let rhsRating = personalRating(for: rhs)
                switch (lhsRating, rhsRating) {
                case let (left?, right?) where left != right:
                    return left > right
                case (_?, nil):
                    return true
                case (nil, _?):
                    return false
                default:
                    let lhsRank = lhs.rank > 0 ? lhs.rank : Int.max
                    let rhsRank = rhs.rank > 0 ? rhs.rank : Int.max
                    if lhsRank != rhsRank { return lhsRank < rhsRank }
                    return lhs.savedAt > rhs.savedAt
                }
            }
        }
    }

    var body: some View {
        Group {
            if profileSaved.isEmpty {
                ContentUnavailableView("No saved recipes yet", systemImage: "heart", description: Text("Swipe right in Discover when something looks worth trying."))
            } else {
                List {
                    Section {
                        Button("Help Me Pick", systemImage: "shuffle") { showPicker = true }
                            .disabled(profileSaved.filter { $0.status == .wantToTry }.count < 2)
                    }

                    if saved.isEmpty {
                        Section {
                            ContentUnavailableView(
                                "No recipes match",
                                systemImage: "line.3.horizontal.decrease.circle",
                                description: Text("Change the current search or filters to see saved recipes.")
                            )
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        ForEach(saved) { record in
                            NavigationLink {
                                SavedRecipeDetailView(saved: record)
                            } label: {
                                SavedRecipeRow(saved: record, personalRating: personalRating(for: record))
                            }
                        }
                    }
                }
                .listStyle(.insetGrouped)
                .scrollContentBackground(.hidden)
            }
        }
        .recipeScreenBackground()
        .navigationTitle("Saved")
        .searchable(text: $searchText, prompt: "Search saved recipes")
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                Menu("Sort", systemImage: "arrow.up.arrow.down") {
                    Picker("Sort by", selection: $sortOption) {
                        ForEach(SavedSortOption.allCases) { option in
                            Text(option.rawValue).tag(option)
                        }
                    }
                }
                .accessibilityIdentifier("saved.sort")

                Menu("Filter", systemImage: "line.3.horizontal.decrease.circle") {
                    Picker("Status", selection: $statusFilter) {
                        Text("All statuses").tag("All")
                        ForEach(SavedRecipeStatus.allCases) { Text($0.rawValue).tag($0.rawValue) }
                    }
                    Picker("Vertical", selection: $verticalFilter) {
                        Text("All verticals").tag("All")
                        ForEach(Array(Set(profileSaved.map(\.verticalName))).sorted(), id: \.self) { Text($0).tag($0) }
                    }
                }
                .accessibilityIdentifier("saved.filter")
            }
        }
        .sheet(isPresented: $showPicker) {
            NavigationStack {
                EliminationView(recipes: profileSaved.filter { $0.status == .wantToTry })
            }
        }
        .recipeToolbarBehavior()
    }

    private func personalRating(for record: SavedRecipeRecord) -> Int? {
        allReviews.first {
            $0.profileID == appModel.activeProfileID && $0.recipeID == record.recipeID
        }?.overall
    }
}

private struct SavedRecipeRow: View {
    let saved: SavedRecipeRecord
    let personalRating: Int?

    var body: some View {
        HStack(spacing: 12) {
            RemoteRecipeImage(url: saved.imageURL, title: saved.title)
                .frame(width: 78, height: 78)
                .clipped()
                .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            VStack(alignment: .leading, spacing: 5) {
                Text(saved.title).font(.headline).lineLimit(2)
                Text("#\(saved.rank) \(saved.verticalName) · \(saved.rating, specifier: "%.1f") ★")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                if let personalRating {
                    Text("Your rating \(personalRating)/5")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(RecipeDesign.accent)
                }
                Text(saved.status.rawValue)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 3)
        .accessibilityElement(children: .combine)
    }
}

struct SavedRecipeDetailView: View {
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.openURL) private var openURL
    @Query(sort: \PersonalNoteRecord.createdAt, order: .reverse) private var allNotes: [PersonalNoteRecord]
    @Query(sort: \PersonalReviewRecord.createdAt, order: .reverse) private var allReviews: [PersonalReviewRecord]
    let saved: SavedRecipeRecord
    @State private var noteText = ""
    @State private var showReview = false
    @State private var showPlan = false
    @State private var planDate = Date.now

    private var notes: [PersonalNoteRecord] { allNotes.filter { $0.recipeID == saved.recipeID && $0.profileID == appModel.activeProfileID } }
    private var reviews: [PersonalReviewRecord] { allReviews.filter { $0.recipeID == saved.recipeID && $0.profileID == appModel.activeProfileID } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                RemoteRecipeImage(url: saved.imageURL, title: saved.title)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(1.18, contentMode: .fill)
                    .clipped()
                    .clipShape(RoundedRectangle(cornerRadius: RecipeDesign.cornerRadius, style: .continuous))

                VStack(alignment: .leading, spacing: 10) {
                    Text(saved.title)
                        .font(.largeTitle.bold())
                        .fixedSize(horizontal: false, vertical: true)
                    Text("#\(saved.rank) \(saved.verticalName) · \(saved.rating, specifier: "%.1f") ★ · \(saved.ratingCount.formatted()) ratings")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)

                    Picker("Status", selection: Binding(
                        get: { saved.status },
                        set: { appModel.setStatus($0, for: saved) }
                    )) {
                        ForEach(SavedRecipeStatus.allCases) { Text($0.rawValue).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                .padding(18)
                .frame(maxWidth: .infinity, alignment: .leading)
                .recipeGlassSurface()

                RecipeGlassGroup(spacing: 12) {
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 12) {
                            Button("Plan", systemImage: "calendar.badge.plus") { showPlan = true }
                                .recipeGlassButton()
                            Button("I Cooked This", systemImage: "fork.knife") {
                                appModel.markCooked(saved)
                                showReview = true
                            }
                            .recipeGlassButton(prominent: true)
                            Button(saved.status == .favorite ? "Unfavorite" : "Favorite", systemImage: "heart.fill") { appModel.toggleFavorite(saved) }
                                .recipeGlassButton()
                        }

                        VStack(spacing: 10) {
                            Button("Plan", systemImage: "calendar.badge.plus") { showPlan = true }
                                .recipeGlassButton()
                            Button("I Cooked This", systemImage: "fork.knife") {
                                appModel.markCooked(saved)
                                showReview = true
                            }
                            .recipeGlassButton(prominent: true)
                            Button(saved.status == .favorite ? "Unfavorite" : "Favorite", systemImage: "heart.fill") { appModel.toggleFavorite(saved) }
                                .recipeGlassButton()
                        }
                    }
                    .frame(maxWidth: .infinity)
                }

                if !saved.ingredients.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        sectionTitle("Ingredients", systemImage: "carrot")
                        ForEach(Array(saved.ingredients.enumerated()), id: \.offset) { _, ingredient in
                            Text("• \(ingredient)")
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }

                VStack(alignment: .leading, spacing: 12) {
                    sectionTitle("Private notes", systemImage: "note.text")
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .bottom) {
                            TextField("What would you change next time?", text: $noteText, axis: .vertical)
                                .textFieldStyle(.roundedBorder)
                            Button("Add") {
                                appModel.addNote(to: saved, text: noteText)
                                noteText = ""
                            }
                            .recipeGlassButton(prominent: true)
                        }

                        VStack(alignment: .leading, spacing: 8) {
                            TextField("What would you change next time?", text: $noteText, axis: .vertical)
                                .textFieldStyle(.roundedBorder)
                            Button("Add") {
                                appModel.addNote(to: saved, text: noteText)
                                noteText = ""
                            }
                            .recipeGlassButton(prominent: true)
                        }
                    }
                    ForEach(notes) { note in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(note.text)
                            Text(note.createdAt, format: .dateTime.month().day().year())
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 4)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                if let latest = reviews.first {
                    VStack(alignment: .leading, spacing: 8) {
                        sectionTitle("Your latest review", systemImage: "star.bubble")
                        Text("Overall \(latest.overall)/5 · Taste \(latest.taste)/5 · Ease \(latest.ease)/5 · Value \(latest.value)/5")
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Make again: \(latest.wouldMakeAgain.rawValue)")
                        if !latest.householdReaction.isEmpty { Text("Household: \(latest.householdReaction)") }
                        if !latest.notes.isEmpty { Text(latest.notes).foregroundStyle(.secondary) }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .recipeGlassSurface(cornerRadius: RecipeDesign.compactCornerRadius)
                }

                if let url = saved.sourceURL {
                    Button("View Original Recipe", systemImage: "arrow.up.right.square") {
                        appModel.recordOriginalSourceOpened(recipeID: saved.recipeID, verticalID: saved.verticalID)
                        openURL(url)
                    }
                    .recipeGlassButton()
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
            .padding(.bottom, 72)
        }
        .recipeScreenBackground()
        .navigationTitle("Saved Recipe")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showReview) { ReviewFormView(saved: saved) }
        .sheet(isPresented: $showPlan) {
            NavigationStack {
                Form {
                    DatePicker("Cook on", selection: $planDate, displayedComponents: .date)
                    Button("Add to Plan") {
                        appModel.planRecipe(saved, on: planDate)
                        showPlan = false
                    }
                }
                .navigationTitle("Plan Recipe")
            }
        }
        .recipeToolbarBehavior()
    }

    private func sectionTitle(_ title: String, systemImage: String) -> some View {
        Label(title, systemImage: systemImage)
            .font(.title2.bold())
            .accessibilityAddTraits(.isHeader)
    }
}
