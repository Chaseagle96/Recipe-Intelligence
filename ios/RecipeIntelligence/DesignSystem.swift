import SwiftUI
import UIKit

enum RecipeDesign {
    static let accent = Color.orange
    static let warmAccent = Color.yellow
    static let cornerRadius: CGFloat = 28
    static let compactCornerRadius: CGFloat = 18
    static let contentSpacing: CGFloat = 16
}

struct RecipeBackdrop: View {
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground)
            RadialGradient(
                colors: [
                    RecipeDesign.accent.opacity(colorScheme == .dark ? 0.20 : 0.12),
                    .clear
                ],
                center: .topLeading,
                startRadius: 12,
                endRadius: 540
            )
            RadialGradient(
                colors: [
                    RecipeDesign.warmAccent.opacity(colorScheme == .dark ? 0.10 : 0.07),
                    .clear
                ],
                center: .bottomTrailing,
                startRadius: 20,
                endRadius: 460
            )
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}

struct RecipeGlassGroup<Content: View>: View {
    private let spacing: CGFloat
    private let content: Content

    init(spacing: CGFloat = 12, @ViewBuilder content: () -> Content) {
        self.spacing = spacing
        self.content = content()
    }

    @ViewBuilder
    var body: some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: spacing) {
                content
            }
        } else {
            content
        }
    }
}

private struct RecipeGlassSurfaceModifier: ViewModifier {
    let cornerRadius: CGFloat
    let tint: Color?
    let interactive: Bool

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            if let tint {
                content.glassEffect(
                    .regular.tint(tint).interactive(interactive),
                    in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                )
            } else {
                content.glassEffect(
                    .regular.interactive(interactive),
                    in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                )
            }
        } else {
            content
                .background(
                    Color(uiColor: .secondarySystemBackground).opacity(0.94),
                    in: RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                )
                .overlay {
                    RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                        .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                }
        }
    }
}

private struct RecipeGlassButtonModifier: ViewModifier {
    let prominent: Bool

    @ViewBuilder
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            if prominent {
                content.buttonStyle(.glassProminent)
            } else {
                content.buttonStyle(.glass)
            }
        } else {
            if prominent {
                content.buttonStyle(.borderedProminent)
            } else {
                content.buttonStyle(.bordered)
            }
        }
    }
}

extension View {
    func recipeGlassSurface(
        cornerRadius: CGFloat = RecipeDesign.cornerRadius,
        tint: Color? = nil,
        interactive: Bool = false
    ) -> some View {
        modifier(RecipeGlassSurfaceModifier(cornerRadius: cornerRadius, tint: tint, interactive: interactive))
    }

    func recipeGlassButton(prominent: Bool = false) -> some View {
        modifier(RecipeGlassButtonModifier(prominent: prominent))
    }

    @ViewBuilder
    func recipeTabBarBehavior() -> some View {
        if #available(iOS 26.0, *) {
            tabBarMinimizeBehavior(.onScrollDown)
        } else {
            self
        }
    }

    // Keep the navigation bar in its standard safe-area layout. On iOS 27,
    // navigation-bar minimization can float over the first scroll-content row,
    // leaving top controls visible to accessibility but not hittable.
    @ViewBuilder
    func recipeToolbarBehavior() -> some View {
        self
    }

    func recipeScreenBackground() -> some View {
        background(RecipeBackdrop())
    }
}

struct RecipeMetricPill: View {
    let title: String
    let systemImage: String

    var body: some View {
        Label(title, systemImage: systemImage)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 11)
            .padding(.vertical, 8)
            .recipeGlassSurface(cornerRadius: 16)
            .accessibilityElement(children: .combine)
    }
}
