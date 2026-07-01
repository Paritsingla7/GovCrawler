# india.gov.in Web Directory API Documentation

This document describes the internal Next.js API routes used by the `india.gov.in` directory pages, which are utilized
by the GovCrawler scraper.

## Overview

The directory pages (e.g., `/directory/web-directory`) load data via POST JSON APIs. These APIs act as proxies to a
GraphQL backend. Since they are same-origin Next.js routes, they do not have standard bot detection or CAPTCHAs, making
them ideal for scraping.

### Endpoints

- **Web Directory API**: `https://www.india.gov.in/directory/web-directory/api`

### Common Headers

To mimic legitimate requests, the following headers are generally used:

```http
User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Content-Type: application/json
Referer: https://www.india.gov.in/directory/web-directory
Origin: https://www.india.gov.in
```

## 1. Fetching Categories

Retrieves all available directory categories and the count of entries in each.

- **URL**: `https://www.india.gov.in/directory/web-directory/api`
- **Method**: `POST`
- **Body**:

```json
{
  "dataval": {
    "querytype": "Webdirectorycategorywithcounts"
  }
}
```

- **Response Structure**:
  The relevant data is nested deeply within the response.

```json
{
  "resultdata": {
    "data": {
      "getIgodCategoryWithCount": {
        "results": [
          {
            "category": "ug",
            "title": "Union Government",
            "count": 1234
          },
          {
            "category": "sg",
            "title": "State Government",
            "count": 5678
          }
          // ...
        ]
      }
    }
  }
}
```

## 2. Fetching Organization Types (Filters)

Retrieves the available organization type filters for a given category.

- **URL**: `https://www.india.gov.in/directory/web-directory/api`
- **Method**: `POST`
- **Body**:

```json
{
  "dataval": {
    "clientvalue": "client",
    "mustvalue": "<cat_code>",
    "querytype": "organizationtypewithCategory"
  }
}
```

- **Response Structure**:

```json
{
  "resultdata": {
    "data": {
      "getIgodOrganizationByCategory": {
        "total": 6047,
        "results": [
          {
            "title": "Statutory / Autonomous Bodies",
            "organization_type": "E051",
            "count": 227,
            "__typename": "Organization"
          }
          // ...
        ]
      }
    }
  }
}
```

## 3. Fetching Entries for a Category (Paginated)

Retrieves the actual directory entries (including URLs) for a specific category code (e.g., `ug`, `sg`). This endpoint
requires pagination.

- **URL**: `https://www.india.gov.in/directory/web-directory/api`
- **Method**: `POST`
- **Body**:

```json
{
  "dataval": {
    "clientvalue": "client",
    "mustvalue": [
      {
        "fieldName": "category",
        "fieldValue": "<cat_code>" 
      },
      {
        "fieldName": "organization_type",
        "fieldValue": "<org_type_code>"
      }
    ],
    "shouldvalue": [],
    "pageno": 1,
    "pageSize": 100,
    "querytype": "WebdirectoryCategorydetalsList"
  }
}
```

*Replace `<cat_code>` with the actual category code (e.g., "ug"). The `organization_type` filter object is
optional. `pageSize` 100 is known to be stable.*

- **Response Structure**:

```json
{
  "resultdata": {
    "data": {
      "getIgodWebDirectoryByFilters": {
        "total": 1234,
        "results": [
          {
            "url": "https://example.gov.in",
            "title": "Example Ministry"
            // ... other fields
          }
        ]
      }
    }
  }
}
```

- **`url`**: The main website URL of the entity.

## Notes

- The target domains of interest are typically those ending in `.gov.in` and `.nic.in`.
- The GraphQL backend uses `mustvalue` and `shouldvalue` for filtering, suggesting a search engine like Elasticsearch
  might be behind the GraphQL layer.
- If the API routes fail or change, `india.gov.in` uses React Server Components (RSC). Passing `RSC: 1` and
  `Next-Url: <path>` headers to the actual page paths (e.g., `/directory/web-directory/union-government`) can retrieve
  raw RSC streams containing the data as a fallback.
