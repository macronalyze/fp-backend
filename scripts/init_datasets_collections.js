// Initialize the `datasets` and `dataset_observations` collections.
//
// Idempotent: safe to run repeatedly. Creates collections with JSON Schema
// validators on first run, updates validators with `collMod` on subsequent
// runs, and ensures the required indexes exist.
//
// Usage:
//   mongosh "$MONGO_URI" --file scripts/init_datasets_collections.js
//   # or, from inside mongosh after `use bhav`:
//   load("scripts/init_datasets_collections.js");

const DB_NAME = "bhav";
const target = db.getSiblingDB(DB_NAME);

// ── datasets ────────────────────────────────────────────────────────────────
//
// One document per (country, datasetId). Holds descriptive metadata only;
// time-series values live in `dataset_observations`. Catalog summary fields
// (latestPeriod, latestGrowth, cumulativeGrowth, ...) are computed on read
// when observations exist; otherwise the API falls back to optional static
// hints (latestPeriod, latestGrowth, status, cumulativeGrowth, ...) stored
// directly on the meta document. This keeps catalog-only datasets (no
// time-series in DB yet) renderable.
//
const datasetsValidator = {
  $jsonSchema: {
    bsonType: "object",
    required: ["_id", "id", "country", "name", "shortName", "icon"],
    properties: {
      _id: { bsonType: "string", description: "{country}:{datasetId}" },
      id: { bsonType: "string" },
      country: { bsonType: "string" },
      name: { bsonType: "string" },
      shortName: { bsonType: "string" },
      icon: { bsonType: "string" },
      baseYear: { bsonType: "string" },
      baseValue: { bsonType: ["int", "double"] },
      source: { bsonType: "string" },
      description: { bsonType: "string" },
      releaseDate: { bsonType: ["string", "null"] },
      nextRelease: { bsonType: ["string", "null"] },
      latestPeriod: { bsonType: ["string", "null"] },
      latestGrowth: { bsonType: ["double", "int", "null"] },
      cumulativeGrowth: { bsonType: ["double", "int", "null"] },
      cumulativePeriod: { bsonType: ["string", "null"] },
      status: { bsonType: ["string", "null"] },
      sectors: {
        bsonType: "array",
        items: {
          bsonType: "object",
          required: ["id", "name"],
          properties: {
            id: { bsonType: "string" },
            name: { bsonType: "string" },
            weight: { bsonType: ["double", "int", "null"] }
          }
        }
      },
      commodities: {
        bsonType: "array",
        items: { bsonType: "string" }
      }
    }
  }
};

// ── dataset_observations ────────────────────────────────────────────────────
//
// One document per (country, datasetId, period). `period` is "YYYY-MM" for
// monthly observations and "YYYY-YY" (fiscal year) for yearly observations.
// `granularity` discriminates the two. Each doc carries either index/growth
// pairs (e.g. ICI sector indices) or a generic `values` object (e.g. trade
// commodity USD figures); validators leave both groups optional.
//
const observationsValidator = {
  $jsonSchema: {
    bsonType: "object",
    required: ["_id", "country", "datasetId", "granularity", "period"],
    properties: {
      _id: { bsonType: "string", description: "{country}:{datasetId}:{period}" },
      country: { bsonType: "string" },
      datasetId: { bsonType: "string" },
      granularity: { enum: ["monthly", "yearly"] },
      period: { bsonType: "string" },
      label: { bsonType: "string" },
      provisional: { bsonType: "bool" },
      index: { bsonType: "object" },
      growth: { bsonType: "object" },
      values: { bsonType: "object" }
    }
  }
};

function ensureCollection(name, validator) {
  const exists = target.getCollectionNames().indexOf(name) !== -1;
  if (!exists) {
    target.createCollection(name, {
      validator: validator,
      validationLevel: "moderate",
      validationAction: "error"
    });
    print(`created collection: ${name}`);
  } else {
    target.runCommand({
      collMod: name,
      validator: validator,
      validationLevel: "moderate",
      validationAction: "error"
    });
    print(`updated validator: ${name}`);
  }
}

ensureCollection("datasets", datasetsValidator);
ensureCollection("dataset_observations", observationsValidator);

// ── Indexes ─────────────────────────────────────────────────────────────────
//
// `_id` already enforces uniqueness on both collections, so no extra unique
// indexes are needed. The compound index on observations supports:
//   - detail fetch (all observations for a dataset, sorted by period)
//   - granularity filtering (monthly vs yearly)
//   - partial range queries (period >= X, period <= Y)
// The country index on datasets supports the catalog endpoint.
//
target.datasets.createIndex({ country: 1 }, { name: "country_1" });
target.dataset_observations.createIndex(
  { country: 1, datasetId: 1, granularity: 1, period: 1 },
  { name: "country_dataset_granularity_period_1" }
);

print("");
print("indexes on datasets:");
printjson(target.datasets.getIndexes().map(i => ({ name: i.name, key: i.key })));
print("indexes on dataset_observations:");
printjson(target.dataset_observations.getIndexes().map(i => ({ name: i.name, key: i.key })));
