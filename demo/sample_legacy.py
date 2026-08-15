"""
Sample Legacy Python Module - Inventory Management System
This file contains Python 2 style patterns for demo purposes.
"""
import datetime
import os
import sys

# Global configuration
CONFIG = {
    "tax_rate": 0.08,
    "discount_threshold": 100,
    "currency": "USD"
}


def calc_price(qty, unit_price, discount):
    total = qty * unit_price
    if discount != None:
        total = total - discount
    if total < 0:
        total = 0
    print("Calculated price: %s" % str(total))
    return total


def apply_tax(amount):
    tax = amount * CONFIG["tax_rate"]
    print("Tax amount: %s" % str(tax))
    return tax


def format_currency(amount, currency="USD"):
    return "%s %.2f" % (currency, amount)


def validate_inventory(item_id, quantity):
    if quantity <= 0:
        print("Invalid quantity")
        return False
    if item_id == None:
        print("Missing item ID")
        return False
    return True


def calculate_bulk_discount(items):
    total = 0
    for item in items:
        try:
            total = total + item["price"] * item["qty"]
        except:
            print("Error processing item")
            continue
    if total > CONFIG["discount_threshold"]:
        return total * 0.05
    return 0


class InventoryItem:
    def __init__(self, item_id, name, price, stock):
        self.item_id = item_id
        self.name = name
        self.price = price
        self.stock = stock
        self.created_at = datetime.datetime.now()
        self.history = []

    def sell(self, amount, discount=None):
        if amount > self.stock:
            print("Insufficient stock for %s" % self.name)
            return None
        total = calc_price(amount, self.price, discount)
        tax = apply_tax(total)
        final = total + tax
        self.stock = self.stock - amount
        self.history.append({
            "action": "sell",
            "amount": amount,
            "total": final,
            "date": datetime.datetime.now()
        })
        print("Sold %d units of %s for %s" % (amount, self.name, format_currency(final)))
        return final

    def restock(self, amount, supplier="default"):
        if amount <= 0:
            print("Invalid restock amount")
            return False
        self.stock = self.stock + amount
        self.history.append({
            "action": "restock",
            "amount": amount,
            "supplier": supplier,
            "date": datetime.datetime.now()
        })
        print("Restocked %d units of %s" % (amount, self.name))
        return True

    def get_info(self):
        info = "Item: %s\nID: %s\nPrice: %s\nStock: %d" % (
            self.name,
            self.item_id,
            format_currency(self.price),
            self.stock
        )
        return info


class InventoryManager:
    def __init__(self):
        self.items = {}
        self.log_file = "/tmp/inventory.log"

    def add_item(self, item):
        self.items[item.item_id] = item
        self._log("Added item: %s" % item.name)

    def remove_item(self, item_id):
        if item_id in self.items:
            del self.items[item_id]
            self._log("Removed item: %s" % str(item_id))
            return True
        print("Item not found")
        return False

    def get_total_value(self):
        total = 0
        for item_id in self.items:
            item = self.items[item_id]
            total = total + (item.price * item.stock)
        return total

    def _log(self, message):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = "[%s] %s\n" % (timestamp, message)
        try:
            f = open(self.log_file, "a")
            f.write(log_entry)
            f.close()
        except:
            print("Failed to write log: %s" % message)


def generate_report(manager):
    print("=== Inventory Report ===")
    print("Total items: %d" % len(manager.items))
    print("Total value: %s" % format_currency(manager.get_total_value()))
    for item_id in manager.items:
        item = manager.items[item_id]
        print(item.get_info())
        print("---")


def export_to_csv(manager, filepath):
    try:
        f = open(filepath, "w")
        f.write("ID,Name,Price,Stock\n")
        for item_id in manager.items:
            item = manager.items[item_id]
            f.write("%s,%s,%.2f,%d\n" % (item.item_id, item.name, item.price, item.stock))
        f.close()
        print("Exported to %s" % filepath)
        return True
    except:
        print("Export failed")
        return False