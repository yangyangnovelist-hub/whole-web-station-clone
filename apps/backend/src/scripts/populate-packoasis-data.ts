import { ExecArgs } from "@medusajs/framework/types";
import { createProductCategoriesWorkflow } from "@medusajs/medusa/core-flows";

export default async function populatePackOasisData({ container }: ExecArgs) {
  const categoriesToCreate = [
    { name: "Custom Packaging", handle: "custom-packaging" },
    { name: "Custom Automotive Packaging Boxes", handle: "custom-automotive-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Bakery Packaging Boxes", handle: "custom-bakery-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Beer & Liquor Packaging", handle: "custom-beer-liquor-packaging", parent_id: "custom-packaging" },
    { name: "Custom Beverage Packaging Boxes", handle: "custom-beverage-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Candle Packaging Boxes", handle: "custom-candle-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Candy Packaging", handle: "custom-candy-packaging", parent_id: "custom-packaging" },
    { name: "Custom Cannabis Packaging Boxes", handle: "custom-cannabis-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Cardboard Displays", handle: "custom-cardboard-displays", parent_id: "custom-packaging" },
    { name: "Custom Chocolate Packaging Boxes", handle: "custom-chocolate-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Coffee Packaging", handle: "custom-coffee-packaging", parent_id: "custom-packaging" },
    { name: "Custom Cosmetic Packaging Boxes", handle: "custom-cosmetic-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Eco-Friendly Packaging", handle: "custom-eco-friendly-packaging", parent_id: "custom-packaging" },
    { name: "Custom Ecommerce Packaging Boxes", handle: "custom-ecommerce-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Electronics Packaging Boxes", handle: "custom-electronics-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Fashion & Apparel Boxes", handle: "custom-fashion-apparel-boxes", parent_id: "custom-packaging" },
    { name: "Custom Food Packaging Boxes", handle: "custom-food-packaging-boxes", parent_id: "custom-packaging" },
    { name: "Custom Game Boxes", handle: "custom-game-boxes", parent_id: "custom-packaging" },
    { name: "Custom Gift Boxes", handle: "custom-gift-boxes", parent_id: "custom-packaging" },
    { name: "Custom Jewelry & Accessories Boxes", handle: "custom-jewelry-accessories-boxes", parent_id: "custom-packaging" },
  ];

  console.log("Populating PackOasis Categories...");

  // 1. Create Parent Category First
  const { result: parentResult } = await createProductCategoriesWorkflow(container).run({
    input: {
      product_categories: [
        { name: "Custom Packaging", handle: "custom-packaging", is_active: true }
      ]
    }
  });

  const parentId = parentResult[0].id;

  // 2. Create Children
  await createProductCategoriesWorkflow(container).run({
    input: {
      product_categories: categoriesToCreate.filter(c => c.parent_id).map(c => ({
        name: c.name,
        handle: c.handle,
        parent_category_id: parentId,
        is_active: true
      }))
    }
  });

  console.log("PackOasis Categories Populated Successfully.");
}
