"""Correlator (week 6): alerts in, incidents out.

A database fault makes PostgreSQL slow, then the catalog, then the gateway's
product lookups, and every detector fires on its own component. The correlator
turns that into one incident: alerts that start close together in time, on
components connected in the service graph, are grouped. The alert noise metric
(raw alerts per incident, before and after) measures how well it does.
"""
