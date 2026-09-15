import Foundation
import SwiftUI
import UIKit

struct DiscoverView: View {
    @EnvironmentObject private var appModel: AppModel
    @AppStorage("discover.dismissedFeedStatusMessage") private var dismissedFeedStatusMessage = ""
    @StateObject private var ingredientFeed = IngredientFeedModel()
    @State private var isIngredientPickerPresented = false

    private var activeIngredient: String? {
        ingredientFeed.activeIngredient
    }

    private var activeRecipes: [RemoteRecipe] {
        activeIngredient == nil ? appModel.deck : ingredientFeed.recipes
    }

    private var isCurrentFeedLoading: Bool {
        activeIngredient == nil ? appModel.isLoading : ingredientFeed.isLoading
    }

    var body: some View {
        GeometryReader { proxy in
            let horizontalPadding: CGFloat = 16
            let verticalPadding: CGFloat = 8
            let selectorHeight: CGFloat = 62
            let spacing: CGFloat = 12
            let cardWidth = max(0, proxy.size.width - (horizontalPadding * 2))
            let cardHeight = max(
                0,
                proxy.size.height - selectorHeight - spacing - (verticalPadding * 2)
            )

            VStack(spacing: spacing) {
                Group {
                    if isCurrentFeedLoading && activeRecipes.isEmpty {
                        ProgressView(
                            activeIngredient.map { "Finding \($0) recipes…" }
                                ?? "Finding great recipes…"
                        )
                        .controlSize(.large)
                        .frame(width: cardWidth, height: cardHeight)
                        .recipeGlassSurface(cornerRadius: 30)
                    } else if let recipe = activeRecipes.first {
                        deck(recipe, cardWidth: cardWidth, cardHeight: cardHeight)
                            .task(id: recipe.recipeID) {
                                if activeIngredient == nil {
                                    await appModel.prefetchIfNeeded(recipe)
                                }
                            }
                    } else {
                        emptyState
                            .frame(width: cardWidth, height: cardHeight)
                    }
                }
                .frame(width: cardWidth, height: cardHeight)
                .clipped()

                feedSelector
                    .frame(width: cardWidth, height: selectorHeight)
                    .clipped()
            }
            .frame(
                width: cardWidth,
                height: max(0, proxy.size.height - (verticalPadding * 2)),
                alignment: .top
            )
            .padding(.horizontal, horizontalPadding)
            .padding(.vertical, verticalPadding)
            .overlay(alignment: .top) {
                refreshStatus
                    .frame(width: cardWidth)
                    .clipped()
                    .padding(.top, 4)
            }
            .clipped()
        }
        .clipped()
        .recipeScreenBackground()
        .navigationTitle("Discover")
        .navigationBarTitleDisplayMode(.inline)
        .background {
            ShakeUndoDetector(isEnabled: appModel.canUndo) {
                undoLastDecision()
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .sheet(isPresented: $isIngredientPickerPresented) {
            IngredientPickerSheet(
                selectedIngredient: activeIngredient,
                onApply: applyIngredient,
                onClear: clearIngredient
            )
        }
        .animation(.snappy, value: activeRecipes.first?.recipeID)
        .recipeToolbarBehavior()
    }

    @ViewBuilder
    private var refreshStatus: some View {
        if let activeIngredient, ingredientFeed.isLoading {
            Label("Finding \(activeIngredient) recipes…", systemImage: "magnifyingglass")
                .font(.footnote.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
                .recipeGlassSurface(cornerRadius: 16)
                .accessibilityIdentifier("discover.refreshStatus")
        } else if activeIngredient == nil, appModel.isRefreshingFeed {
            Label("Checking for new rankings…", systemImage: "arrow.triangle.2.circlepath")
                .font(.footnote.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
                .recipeGlassSurface(cornerRadius: 16)
                .accessibilityIdentifier("discover.refreshStatus")
        } else if activeIngredient == nil,
                  let message = appModel.feedStatusMessage,
                  message != dismissedFeedStatusMessage {
            HStack(spacing: 10) {
                Text(message)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)

                Button {
                    dismissedFeedStatusMessage = message
                } label: {
                    Image(systemName: "xmark")
                        .font(.caption.bold())
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Dismiss ranking notice")
                .accessibilityIdentifier("discover.dismissRefreshStatus")
            }
            .padding(.leading, 12)
            .padding(.trailing, 6)
            .padding(.vertical, 6)
            .frame(maxWidth: .infinity)
            .recipeGlassSurface(cornerRadius: 16)
            .accessibilityIdentifier("discover.refreshStatus")
        }
    }

    private var feedSelector: some View {
        RecipeGlassGroup(spacing: 8) {
            HStack(spacing: 8) {
                Menu {
                    ForEach(appModel.verticals) { vertical in
                        Button {
                            Task { await selectMethod(vertical) }
                        } label: {
                            if appModel.selectedVertical?.id == vertical.id {
                                Label(vertical.name, systemImage: "checkmark")
                            } else {
                                Label(vertical.name, systemImage: vertical.icon)
                            }
                        }
                        .accessibilityIdentifier("method.\(vertical.id)")
                    }
                } label: {
                    filterLabel(
                        title: "Method",
                        value: appModel.selectedVertical?.name ?? "Choose",
                        systemImage: "slider.horizontal.3",
                        showsChevron: true,
                        selected: true
                    )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Method, \(appModel.selectedVertical?.name ?? "Choose method")")
                .accessibilityIdentifier("discover.method")

                Button {
                    isIngredientPickerPresented = true
                } label: {
                    filterLabel(
                        title: "Ingredient",
                        value: activeIngredient ?? "Any",
                        systemImage: "carrot.fill",
                        showsChevron: true,
                        selected: activeIngredient != nil
                    )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Ingredient, \(activeIngredient ?? "Any")")
                .accessibilityIdentifier("discover.ingredient")
            }
        }
        .padding(.horizontal, 2)
        .padding(.vertical, 4)
        .accessibilityIdentifier("discover.feedSelector")
    }

    private func filterLabel(
        title: String,
        value: String,
        systemImage: String,
        showsChevron: Bool,
        selected: Bool
    ) -> some View {
        HStack(spacing: 9) {
            Image(systemName: systemImage)
                .font(.body.weight(.semibold))

            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(value)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.78)
            }

            Spacer(minLength: 2)

            if showsChevron {
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, minHeight: 54)
        .recipeGlassSurface(
            cornerRadius: 22,
            tint: selected ? RecipeDesign.accent.opacity(0.20) : nil,
            interactive: true
        )
    }

    private func selectMethod(_ vertical: RecipeVertical) async {
        await appModel.selectVertical(vertical)
        if let activeIngredient {
            await ingredientFeed.load(
                vertical: vertical,
                ingredient: activeIngredient,
                forceRefresh: true
            )
        }
    }

    private func applyIngredient(_ ingredient: String) {
        guard let vertical = appModel.selectedVertical else { return }
        Task {
            await ingredientFeed.load(
                vertical: vertical,
                ingredient: ingredient,
                forceRefresh: true
            )
        }
    }

    private func clearIngredient() {
        ingredientFeed.clear()
    }

    private func undoLastDecision() {
        appModel.undoLastDecision()
        if let restoredRecipe = appModel.deck.first {
            ingredientFeed.restore(restoredRecipe)
        }
    }

    private func deck(
        _ topRecipe: RemoteRecipe,
        cardWidth: CGFloat,
        cardHeight: CGFloat
    ) -> some View {
        RecipeCardView(
            recipe: topRecipe,
            cardWidth: cardWidth,
            cardHeight: cardHeight,
            onDecision: { decision in
                appModel.handleDecision(decision, recipe: topRecipe)
                if activeIngredient != nil {
                    ingredientFeed.remove(topRecipe)
                }
            },
            onOpen: {
                appModel.recordOpened(topRecipe)
            },
            onRefresh: {
                if let activeIngredient,
                   let vertical = appModel.selectedVertical {
                    await ingredientFeed.load(
                        vertical: vertical,
                        ingredient: activeIngredient,
                        forceRefresh: true
                    )
                } else {
                    await appModel.refreshCurrentFeed(trigger: .manual)
                }
            }
        )
        .id(topRecipe.recipeID)
        .frame(width: cardWidth, height: cardHeight)
        .clipped()
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private var emptyState: some View {
        if let activeIngredient {
            ContentUnavailableView {
                Label("No \(activeIngredient) recipes", systemImage: "magnifyingglass")
            } description: {
                Text(
                    ingredientFeed.errorMessage
                        ?? "Recipe Intelligence has not found an eligible \(activeIngredient) recipe for this method yet."
                )
            } actions: {
                Button("Change Ingredient") {
                    isIngredientPickerPresented = true
                }
                .recipeGlassButton(prominent: true)

                Button("Any Ingredient") {
                    clearIngredient()
                }
                .recipeGlassButton()
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .recipeGlassSurface(cornerRadius: 30)
        } else {
            ContentUnavailableView {
                Label("You’re caught up", systemImage: "checkmark.circle")
            } description: {
                Text(appModel.errorMessage ?? "Choose another method, or check back after Recipe Intelligence finds more recipes.")
            } actions: {
                Button("Try Again") {
                    Task { await appModel.retry() }
                }
                .recipeGlassButton(prominent: true)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .recipeGlassSurface(cornerRadius: 30)
        }
    }
}

private struct RecipeCardView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.openURL) private var openURL

    let recipe: RemoteRecipe
    let cardWidth: CGFloat
    let cardHeight: CGFloat
    let onDecision: (RecipeDecision) -> Void
    let onOpen: () -> Void
    let onRefresh: () async -> Void

    @State private var offset: CGSize = .zero
    @State private var pullOffset: CGFloat = 0
    @State private var isFlipped = false
    @State private var isRefreshingFromPull = false
    @StateObject private var instructionLoader = RecipeInstructionsLoader()

    private let pullRefreshThreshold: CGFloat = 84

    private var detailsHeight: CGFloat {
        min(180, max(164, cardHeight * 0.27))
    }

    private var flipAnimation: Animation {
        .smooth(duration: 0.50)
    }

    var body: some View {
        ZStack {
            frontCardFace
                .opacity(isFlipped ? 0 : 1)
                .rotation3DEffect(
                    .degrees(isFlipped ? -180 : 0),
                    axis: (x: 0, y: 1, z: 0),
                    perspective: 0.22
                )
                .scaleEffect(isFlipped ? 0.985 : 1)
                .zIndex(isFlipped ? 0 : 1)

            backCardFace
                .opacity(isFlipped ? 1 : 0)
                .rotation3DEffect(
                    .degrees(isFlipped ? 0 : 180),
                    axis: (x: 0, y: 1, z: 0),
                    perspective: 0.22
                )
                .scaleEffect(isFlipped ? 1 : 0.985)
                .zIndex(isFlipped ? 1 : 0)
        }
        .frame(width: cardWidth, height: cardHeight)
        .clipped()
        .overlay(alignment: offset.width >= 40 ? .topLeading : .topTrailing) {
            if abs(offset.width) >= 40 {
                Text(offset.width > 0 ? "SAVE" : "NOPE")
                    .font(.title.bold())
                    .foregroundStyle(offset.width > 0 ? .green : .red)
                    .padding(24)
                    .rotationEffect(.degrees(offset.width > 0 ? -8 : 8))
            }
        }
        .offset(x: offset.width, y: pullOffset)
        .rotationEffect(.degrees(Double(offset.width / 24)))
        .frame(width: cardWidth, height: cardHeight)
        .contentShape(Rectangle())
        .simultaneousGesture(dragGesture)
        .animation(reduceMotion ? nil : flipAnimation, value: isFlipped)
        .accessibilityIdentifier("discover.card")
        .accessibilityAction(named: "Save") { onDecision(.save) }
        .accessibilityAction(named: "Skip") { onDecision(.skip) }
        .accessibilityAction(named: "Not Now") { onDecision(.notNow) }
        .accessibilityAction(named: "Refresh feed") { triggerRefresh() }
        .accessibilityAction(named: isFlipped ? "Show ranking" : "Show recipe") {
            isFlipped ? showFront() : showRecipe()
        }
    }

    private var frontCardFace: some View {
        frontFace
            .frame(width: cardWidth, height: cardHeight)
            .recipeGlassSurface(cornerRadius: 30, interactive: true)
            .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
            .shadow(color: .black.opacity(0.08), radius: 14, y: 7)
            .clipped()
    }

    private var backCardFace: some View {
        backFace
            .frame(width: cardWidth, height: cardHeight)
            .recipeGlassSurface(cornerRadius: 30, interactive: true)
            .clipShape(RoundedRectangle(cornerRadius: 30, style: .continuous))
            .shadow(color: .black.opacity(0.08), radius: 14, y: 7)
            .clipped()
    }

    private var frontFace: some View {
        VStack(spacing: 0) {
            RemoteRecipeImage(url: recipe.photoURL, title: recipe.title)
                .frame(width: cardWidth, height: max(0, cardHeight - detailsHeight))
                .clipped()

            VStack(alignment: .leading, spacing: 6) {
                Text(recipe.title)
                    .font(.title3.bold())
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)
                    .fixedSize(horizontal: false, vertical: true)
                    .layoutPriority(1)

                Text(metricSummary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.72)

                Text(recipe.source)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)

                if !recipe.categories.isEmpty {
                    Text(recipe.categories.prefix(3).joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                Label("Tap for ingredients & directions", systemImage: "rectangle.on.rectangle.angled")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .frame(width: cardWidth, height: detailsHeight, alignment: .topLeading)
            .clipped()
        }
        .frame(width: cardWidth, height: cardHeight)
        .clipped()
        .contentShape(Rectangle())
        .onTapGesture { showRecipe() }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(recipe.title). \(recipe.verticalName) recipe. Rating \(String(format: "%.1f", recipe.rating)) from \(recipe.ratingCount) ratings. \(recipe.confidenceLabel).")
        .accessibilityHint("Swipe right to save, left to skip, down to refresh the feed, or activate to show ingredients and directions.")
    }

    private var backFace: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                Text(recipe.title)
                    .font(.headline.bold())
                    .lineLimit(2)
                    .minimumScaleFactor(0.82)
                Text("Recipe details · Tap anywhere to return")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 10)
            .frame(width: cardWidth)
            .clipped()

            Divider()
                .opacity(0.35)
                .frame(width: cardWidth)

            ScrollView(.vertical, showsIndicators: true) {
                VStack(alignment: .leading, spacing: 18) {
                    Text(metricSummary)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    recipeSection(title: "Ingredients", systemImage: "basket.fill") {
                        if recipe.ingredients.isEmpty {
                            Text("Ingredients are not available in this feed yet.")
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        } else {
                            ForEach(Array(recipe.ingredients.enumerated()), id: \.offset) { _, ingredient in
                                Label(ingredient, systemImage: "circle.fill")
                                    .symbolRenderingMode(.hierarchical)
                                    .font(.body)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    .accessibilityIdentifier("discover.ingredients")

                    recipeSection(title: "Directions", systemImage: "list.number") {
                        directionsContent
                    }
                    .accessibilityIdentifier("discover.directions")

                    if !recipe.confidenceLabel.isEmpty {
                        Label(recipe.confidenceLabel, systemImage: "checkmark.seal.fill")
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    if let url = recipe.sourceURL {
                        Button {
                            openURL(url)
                        } label: {
                            Label("Open original recipe", systemImage: "arrow.up.right.square")
                                .frame(maxWidth: .infinity)
                        }
                        .recipeGlassButton()
                        .accessibilityIdentifier("discover.original")
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 14)
                .padding(.bottom, 28)
                .frame(width: cardWidth, alignment: .leading)
                .clipped()
            }
            .frame(width: cardWidth)
            .clipped()
            .accessibilityIdentifier("discover.recipeScroll")
        }
        .frame(width: cardWidth, height: cardHeight)
        .clipped()
        .contentShape(Rectangle())
        .simultaneousGesture(
            TapGesture().onEnded { showFront() }
        )
        .accessibilityHint("Tap anywhere on the recipe details card to show the ranking card.")
    }

    @ViewBuilder
    private var directionsContent: some View {
        if !recipe.hasInstructions {
            Text("This source does not currently expose structured cooking directions.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            switch instructionLoader.state {
            case .idle, .loading:
                HStack(spacing: 10) {
                    ProgressView()
                    Text("Loading directions from \(recipe.source)…")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            case .loaded(let steps):
                ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .top, spacing: 10) {
                        Text("\(index + 1)")
                            .font(.caption.bold())
                            .frame(width: 24, height: 24)
                            .recipeGlassSurface(cornerRadius: 12)
                        Text(step)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            case .unavailable:
                Text("Recipe Intelligence detected \(recipe.instructionCount) structured direction\(recipe.instructionCount == 1 ? "" : "s"), but the publisher page did not expose readable steps to the app. Open the original recipe below for the full method.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func recipeSection<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: systemImage)
                .font(.title3.bold())
                .accessibilityAddTraits(.isHeader)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var metricSummary: String {
        var parts = [
            "★ \(String(format: "%.1f", recipe.rating))",
            "\(recipe.ratingCount.formatted()) ratings"
        ]
        if !recipe.evidenceGrade.isEmpty {
            parts.append("Evidence \(recipe.evidenceGrade)")
        }
        return parts.joined(separator: " · ")
    }

    private func showRecipe() {
        guard !isFlipped else { return }
        onOpen()
        if reduceMotion {
            isFlipped = true
        } else {
            withAnimation(flipAnimation) { isFlipped = true }
        }

        Task {
            await instructionLoader.load(from: recipe.sourceURL)
        }
    }

    private func showFront() {
        if reduceMotion {
            isFlipped = false
        } else {
            withAnimation(flipAnimation) { isFlipped = false }
        }
    }

    private var dragGesture: some Gesture {
        DragGesture(minimumDistance: 16)
            .onChanged { value in
                guard !isFlipped else { return }

                let horizontal = value.translation.width
                let vertical = value.translation.height

                if abs(horizontal) > abs(vertical) {
                    pullOffset = 0
                    offset = CGSize(width: horizontal, height: 0)
                } else if vertical > 0 {
                    offset = .zero
                    pullOffset = min(24, vertical * 0.22)
                }
            }
            .onEnded { value in
                guard !isFlipped else {
                    resetOffset()
                    return
                }

                let horizontal = value.translation.width
                let vertical = value.translation.height

                if vertical >= pullRefreshThreshold, abs(vertical) > abs(horizontal) {
                    triggerRefresh()
                    return
                }

                if abs(horizontal) > abs(vertical) {
                    let threshold: CGFloat = 110
                    if horizontal > threshold {
                        complete(.save)
                    } else if horizontal < -threshold {
                        complete(.skip)
                    } else {
                        resetOffset()
                    }
                } else {
                    resetOffset()
                }
            }
    }

    private func triggerRefresh() {
        guard !isFlipped, !isRefreshingFromPull else {
            resetOffset()
            return
        }

        if reduceMotion {
            pullOffset = 0
        } else {
            withAnimation(.smooth(duration: 0.22)) {
                pullOffset = 0
            }
        }

        isRefreshingFromPull = true
        Task { @MainActor in
            await onRefresh()
            isRefreshingFromPull = false
        }
    }

    private func resetOffset() {
        if reduceMotion {
            offset = .zero
            pullOffset = 0
        } else {
            withAnimation(.smooth(duration: 0.24)) {
                offset = .zero
                pullOffset = 0
            }
        }
    }

    private func complete(_ decision: RecipeDecision) {
        pullOffset = 0
        if reduceMotion {
            offset = .zero
            onDecision(decision)
        } else {
            withAnimation(.easeOut(duration: 0.18)) {
                offset.width = decision == .save ? cardWidth : -cardWidth
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.16) {
                onDecision(decision)
                offset = .zero
            }
        }
    }
}

private struct ShakeUndoDetector: UIViewControllerRepresentable {
    let isEnabled: Bool
    let onShake: () -> Void

    func makeUIViewController(context: Context) -> ShakeUndoViewController {
        let controller = ShakeUndoViewController()
        controller.isShakeEnabled = isEnabled
        controller.onShake = onShake
        return controller
    }

    func updateUIViewController(_ uiViewController: ShakeUndoViewController, context: Context) {
        uiViewController.isShakeEnabled = isEnabled
        uiViewController.onShake = onShake
        DispatchQueue.main.async {
            uiViewController.ensureFirstResponder()
        }
    }
}

private final class ShakeUndoViewController: UIViewController {
    var isShakeEnabled = false
    var onShake: (() -> Void)?

    override var canBecomeFirstResponder: Bool { true }

    override func loadView() {
        let view = UIView(frame: .zero)
        view.backgroundColor = .clear
        view.isUserInteractionEnabled = false
        self.view = view
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        ensureFirstResponder()
    }

    override func didMove(toParent parent: UIViewController?) {
        super.didMove(toParent: parent)
        ensureFirstResponder()
    }

    func ensureFirstResponder() {
        guard viewIfLoaded?.window != nil else { return }
        if !isFirstResponder {
            becomeFirstResponder()
        }
    }

    override func motionEnded(_ motion: UIEvent.EventSubtype, with event: UIEvent?) {
        super.motionEnded(motion, with: event)
        guard isShakeEnabled, motion == .motionShake else { return }
        onShake?()
    }
}

@MainActor
private final class RecipeInstructionsLoader: ObservableObject {
    enum State {
        case idle
        case loading
        case loaded([String])
        case unavailable
    }

    @Published private(set) var state: State = .idle

    func load(from url: URL?) async {
        guard case .idle = state else { return }
        guard let url else {
            state = .unavailable
            return
        }

        state = .loading
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15 Version/27.0 Mobile/15E148 Safari/604.1",
            forHTTPHeaderField: "User-Agent"
        )
        request.setValue(
            "text/html,application/xhtml+xml",
            forHTTPHeaderField: "Accept"
        )

        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse,
               !(200..<300).contains(http.statusCode) {
                state = .unavailable
                return
            }

            let steps = await Task.detached(priority: .utility) {
                RecipeInstructionExtractor.extract(from: data)
            }.value
            state = steps.isEmpty ? .unavailable : .loaded(steps)
        } catch {
            state = .unavailable
        }
    }
}

private enum RecipeInstructionExtractor {
    static func extract(from data: Data) -> [String] {
        let html = String(decoding: data, as: UTF8.self)
        let pattern = #"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>"#
        guard let regex = try? NSRegularExpression(
            pattern: pattern,
            options: [.caseInsensitive, .dotMatchesLineSeparators]
        ) else {
            return []
        }

        let fullRange = NSRange(html.startIndex..<html.endIndex, in: html)
        for match in regex.matches(in: html, range: fullRange) {
            guard match.numberOfRanges > 1,
                  let range = Range(match.range(at: 1), in: html) else {
                continue
            }

            let jsonText = String(html[range])
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard let jsonData = jsonText.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: jsonData) else {
                continue
            }

            if let steps = findRecipeSteps(in: object), !steps.isEmpty {
                return deduplicated(steps)
            }
        }
        return []
    }

    private static func findRecipeSteps(in object: Any) -> [String]? {
        if let dictionary = object as? [String: Any] {
            if isRecipeType(dictionary["@type"]) {
                let steps = flattenInstructions(dictionary["recipeInstructions"])
                if !steps.isEmpty {
                    return steps
                }
            }

            if let graph = dictionary["@graph"],
               let steps = findRecipeSteps(in: graph),
               !steps.isEmpty {
                return steps
            }

            for value in dictionary.values {
                if let steps = findRecipeSteps(in: value), !steps.isEmpty {
                    return steps
                }
            }
        } else if let array = object as? [Any] {
            for item in array {
                if let steps = findRecipeSteps(in: item), !steps.isEmpty {
                    return steps
                }
            }
        }
        return nil
    }

    private static func isRecipeType(_ value: Any?) -> Bool {
        if let type = value as? String {
            return type.caseInsensitiveCompare("Recipe") == .orderedSame
        }
        if let types = value as? [String] {
            return types.contains {
                $0.caseInsensitiveCompare("Recipe") == .orderedSame
            }
        }
        return false
    }

    private static func flattenInstructions(_ value: Any?) -> [String] {
        guard let value else { return [] }

        if let text = value as? String {
            let cleaned = clean(text)
            return cleaned.isEmpty ? [] : [cleaned]
        }

        if let array = value as? [Any] {
            return array.flatMap { flattenInstructions($0) }
        }

        if let dictionary = value as? [String: Any] {
            if let text = dictionary["text"] as? String {
                let cleaned = clean(text)
                if !cleaned.isEmpty {
                    return [cleaned]
                }
            }

            if let items = dictionary["itemListElement"] {
                let nested = flattenInstructions(items)
                if !nested.isEmpty {
                    return nested
                }
            }

            if let name = dictionary["name"] as? String {
                let cleaned = clean(name)
                if !cleaned.isEmpty {
                    return [cleaned]
                }
            }
        }

        return []
    }

    private static func clean(_ text: String) -> String {
        let noTags = text.replacingOccurrences(
            of: "<[^>]+>",
            with: " ",
            options: .regularExpression
        )
        let decoded = noTags
            .replacingOccurrences(of: "&amp;", with: "&")
            .replacingOccurrences(of: "&quot;", with: "\"")
            .replacingOccurrences(of: "&#34;", with: "\"")
            .replacingOccurrences(of: "&#39;", with: "'")
            .replacingOccurrences(of: "&apos;", with: "'")
            .replacingOccurrences(of: "&nbsp;", with: " ")

        return decoded
            .replacingOccurrences(
                of: "\\s+",
                with: " ",
                options: .regularExpression
            )
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func deduplicated(_ steps: [String]) -> [String] {
        var seen = Set<String>()
        var result: [String] = []
        for step in steps {
            let key = step.lowercased()
            if seen.insert(key).inserted {
                result.append(step)
            }
        }
        return result
    }
}
