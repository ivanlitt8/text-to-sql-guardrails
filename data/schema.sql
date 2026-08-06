-- Schema de la base de datos de vuelos
-- Motor: DuckDB
-- Ver docs/SPECS.md sección 5 para el detalle de cada tabla

CREATE TABLE aerolineas (
    id INTEGER PRIMARY KEY,
    nombre VARCHAR NOT NULL,
    pais VARCHAR NOT NULL
);

CREATE TABLE ciudades (
    ciudad VARCHAR PRIMARY KEY,
    pais VARCHAR NOT NULL
);

CREATE TABLE vuelos (
    id INTEGER PRIMARY KEY,
    aerolinea_id INTEGER NOT NULL REFERENCES aerolineas(id),
    origen VARCHAR NOT NULL,
    destino VARCHAR NOT NULL,
    fecha DATE NOT NULL,
    duracion_minutos INTEGER NOT NULL,
    precio DECIMAL(10,2) NOT NULL,
    asientos_disponibles INTEGER NOT NULL
);

CREATE TABLE pasajeros (
    id INTEGER PRIMARY KEY,
    nombre VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    pais_residencia VARCHAR NOT NULL
);

CREATE TABLE reservas (
    id INTEGER PRIMARY KEY,
    pasajero_id INTEGER NOT NULL REFERENCES pasajeros(id),
    vuelo_id INTEGER NOT NULL REFERENCES vuelos(id),
    fecha_reserva DATE NOT NULL,
    precio_pagado DECIMAL(10,2) NOT NULL,
    estado VARCHAR NOT NULL CHECK (estado IN ('confirmada', 'cancelada', 'pendiente'))
);
