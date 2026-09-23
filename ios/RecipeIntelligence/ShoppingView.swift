import SwiftData
import SwiftUI

struct ShoppingView: View {
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.modelContext) private var modelContext
    @Query(sort: \ShoppingListItem.createdAt) private var allItems: [ShoppingListItem]
    @State private var manualItem = ""

    private var items: [ShoppingListItem] { allItems.filter { $0.profileID == appModel.activeProfileID } }
    private var categories: [String] { Array(Set(items.map(\.category))).sorted() }
    private var completedCount: Int { items.filter(\.isChecked).count }

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    Label("One list for the whole week", systemImage: "cart.badge.plus")
                        .font(.headline)
                    if !items.isEmpty {
                        Text("\(completedCount) of \(items.count) items checked")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    Button("Build from This Week", systemImage: "wand.and.stars") { appModel.generateShoppingList() }
                        .recipeGlassButton(prominent: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.vertical, 4)

                HStack {
                    TextField("Add an item", text: $manualItem)
                        .textInputAutocapitalization(.sentences)
                    Button("Add") {
                        appModel.addManualShoppingItem(manualItem)
                        manualItem = ""
                    }
                    .disabled(manualItem.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }

            if items.isEmpty {
                Section {
                    ContentUnavailableView(
                        "Your list is empty",
                        systemImage: "cart",
                        description: Text("Plan recipes, then build one combined ingredient list.")
                    )
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 28)
                }
            } else {
                ForEach(categories, id: \.self) { category in
                    Section(category) {
                        ForEach(items.filter { $0.category == category }) { item in
                            Toggle(isOn: Binding(
                                get: { item.isChecked },
                                set: {
                                    item.isChecked = $0
                                    try? modelContext.save()
                                }
                            )) {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(displayText(item))
                                        .fixedSize(horizontal: false, vertical: true)
                                    if item.sourceRecipeIDs.count > 1 {
                                        Text("Used by \(item.sourceRecipeIDs.count) planned recipes")
                                            .font(.caption)
                                            .foregroundStyle(.secondary)
                                    }
                                }
                            }
                            .swipeActions {
                                Button("Delete", role: .destructive) { appModel.deleteShoppingItem(item) }
                            }
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
        .recipeScreenBackground()
        .navigationTitle("Shopping")
        .recipeToolbarBehavior()
    }

    private func displayText(_ item: ShoppingListItem) -> String {
        guard let amount = item.amount else { return item.displayName }
        let amountText = amount.rounded() == amount ? String(Int(amount)) : String(format: "%.2f", amount).replacingOccurrences(of: ".00", with: "")
        return [amountText, item.unit, item.displayName].filter { !$0.isEmpty }.joined(separator: " ")
    }
}
