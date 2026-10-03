"""
Portals own screens; domain apps own data (SRS §6.1).

A portal package contains views, forms, urls and templates and **no models**.
It imports services and selectors, never the ORM directly.
"""
