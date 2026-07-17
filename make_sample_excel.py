"""Generate a sample 10-item auction Excel (item number, item name, starting bid price)."""
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Items"

# Header row — these exact column names are what the app expects on upload.
ws.append(["item_number", "item_name", "starting_bid"])

items = [
    (1, "Vintage Road Bicycle", 120.00),
    (2, "Mechanical Keyboard (RGB)", 60.00),
    (3, "Noise-Cancelling Headphones", 90.00),
    (4, "Espresso Machine", 150.00),
    (5, "4-Person Camping Tent", 80.00),
    (6, "Acoustic Guitar", 110.00),
    (7, "Smart Watch", 130.00),
    (8, "Air Fryer (5.5L)", 55.00),
    (9, "Cordless Drill Kit", 70.00),
    (10, "Bluetooth Speaker", 45.00),
]
for row in items:
    ws.append(row)

# Widen columns a touch for readability
ws.column_dimensions["A"].width = 14
ws.column_dimensions["B"].width = 32
ws.column_dimensions["C"].width = 14

wb.save("items.xlsx")
print("Wrote items.xlsx with", len(items), "items")
