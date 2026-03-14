# DSTAR-P2P

A lightweight peer-to-peer communication concept for amateur radio on D-STAR low-speed data.

**Project Website**  
https://je1hfu.github.io/DSTAR-P2P/

## Overview

DSTAR-P2P is an experimental project that explores peer-to-peer communication over D-STAR low-speed data.
DSTAR-P2P は、D-STARの低速データ通信を用いた、アマチュア無線向けP2P通信の可能性を探る実験プロジェクトです。

The project is designed to enable nearby stations to discover each other, exchange simple status information, and share lightweight location data such as grid locators, without relying on heavy infrastructure.

This project is currently in the prototype and concept validation stage.

## Concept

The basic communication flow is built around a simple multi-step exchange:

1. **CQ**
   - A station announces its presence to nearby stations.

2. **Response**
   - Stations that receive the announcement respond.

3. **QRV?**
   - A station requests more detailed information from a selected station.

4. **Share Info**
   - Stations exchange lightweight information such as grid locator and short status messages.

## Goals

- Explore peer-to-peer communication over D-STAR low-speed data
- Discover nearby active stations
- Exchange short text-based information
- Share lightweight location information using grid locators
- Study simple and resilient communication methods that may also be useful in emergency situations

## Current Status

The project is currently focused on:

- communication flow design
- protocol prototyping
- QRV response logic
- grid locator data handling
- basic software implementation and testing

## Notes

This repository is primarily used as the public-facing project website repository.

The main development repository is managed separately.

## License

No license has been added yet.
