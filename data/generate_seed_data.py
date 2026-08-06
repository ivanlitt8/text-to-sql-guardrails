"""
Genera data/seed_data.sql con datos sintéticos de vuelos.

Se usa un seed fijo para que los datos sean reproducibles: correr este
script siempre genera exactamente el mismo dataset. Si se necesita más
volumen de datos, ajustar las constantes de abajo y volver a correr.

Uso:
    python generate_seed_data.py
"""

import random
from datetime import date, timedelta

random.seed(42)

AEROLINEAS = [
    (1, "Aerolineas Argentinas", "Argentina"),
    (2, "LATAM", "Chile"),
    (3, "Iberia", "España"),
    (4, "American Airlines", "Estados Unidos"),
    (5, "Flybondi", "Argentina"),
]

CIUDADES = [
    "Buenos Aires", "Cordoba", "Bariloche", "Mendoza", "Madrid",
    "Miami", "San Pablo", "Santiago de Chile", "Barcelona", "Nueva York",
]

# País de cada ciudad (alineado a pais_residencia / aerolineas.pais).
CIUDAD_PAIS = {
    "Buenos Aires": "Argentina",
    "Cordoba": "Argentina",
    "Bariloche": "Argentina",
    "Mendoza": "Argentina",
    "Madrid": "España",
    "Barcelona": "España",
    "Miami": "Estados Unidos",
    "Nueva York": "Estados Unidos",
    "San Pablo": "Brasil",
    "Santiago de Chile": "Chile",
}

PAISES_PASAJERO = [
    "Argentina", "Chile", "España", "Estados Unidos", "Brasil", "Uruguay",
]

N_VUELOS = 150
N_PASAJEROS = 80
N_RESERVAS = 300

# Fecha de referencia: hoy en el mundo simulado del dataset.
HOY = date(2026, 8, 2)


def random_fecha(dias_atras_max, dias_adelante_max=0):
    delta = random.randint(-dias_atras_max, dias_adelante_max)
    return HOY + timedelta(days=delta)


def sql_str(s):
    return "'" + s.replace("'", "''") + "'"


lines = []

lines.append("-- Datos sintéticos generados con generate_seed_data.py (seed=42)")
lines.append("-- No editar a mano: volver a correr el script si se necesita otro dataset\n")

# Aerolíneas
lines.append("-- Aerolíneas")
for id_, nombre, pais in AEROLINEAS:
    lines.append(
        f"INSERT INTO aerolineas VALUES ({id_}, {sql_str(nombre)}, {sql_str(pais)});"
    )
lines.append("")

# Ciudades (mapeo ciudad → país para JOINs con destino/origen)
lines.append("-- Ciudades")
for ciudad in CIUDADES:
    pais = CIUDAD_PAIS[ciudad]
    lines.append(
        f"INSERT INTO ciudades VALUES ({sql_str(ciudad)}, {sql_str(pais)});"
    )
lines.append("")

# Vuelos
lines.append("-- Vuelos")
vuelos = []
for vid in range(1, N_VUELOS + 1):
    aerolinea_id = random.choice(AEROLINEAS)[0]
    origen, destino = random.sample(CIUDADES, 2)
    fecha = random_fecha(dias_atras_max=200, dias_adelante_max=60)
    duracion = random.randint(45, 780)
    precio = round(random.uniform(80, 1200), 2)
    asientos = random.randint(0, 180)
    vuelos.append((vid, aerolinea_id, origen, destino, fecha, duracion, precio, asientos))
    lines.append(
        f"INSERT INTO vuelos VALUES ({vid}, {aerolinea_id}, {sql_str(origen)}, "
        f"{sql_str(destino)}, '{fecha.isoformat()}', {duracion}, {precio}, {asientos});"
    )
lines.append("")

# Pasajeros
lines.append("-- Pasajeros")
pasajeros = []
for pid in range(1, N_PASAJEROS + 1):
    nombre = f"Pasajero {pid}"
    email = f"pasajero{pid}@example.com"
    pais = random.choice(PAISES_PASAJERO)
    pasajeros.append(pid)
    lines.append(
        f"INSERT INTO pasajeros VALUES ({pid}, {sql_str(nombre)}, {sql_str(email)}, {sql_str(pais)});"
    )
lines.append("")

# Reservas
lines.append("-- Reservas")
estados = ["confirmada", "confirmada", "confirmada", "cancelada", "pendiente"]
for rid in range(1, N_RESERVAS + 1):
    pasajero_id = random.choice(pasajeros)
    vuelo = random.choice(vuelos)
    vuelo_id, precio_vuelo = vuelo[0], vuelo[6]
    # La reserva se hizo en algún momento antes de la fecha del vuelo,
    # concentrando volumen en el último trimestre para que las preguntas
    # tipo "último trimestre" tengan datos representativos.
    fecha_reserva = random_fecha(dias_atras_max=90, dias_adelante_max=0)
    precio_pagado = round(precio_vuelo * random.uniform(0.9, 1.1), 2)
    estado = random.choice(estados)
    lines.append(
        f"INSERT INTO reservas VALUES ({rid}, {pasajero_id}, {vuelo_id}, "
        f"'{fecha_reserva.isoformat()}', {precio_pagado}, {sql_str(estado)});"
    )

with open("seed_data.sql", "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")

print(f"Generado seed_data.sql con {N_VUELOS} vuelos, {N_PASAJEROS} pasajeros, {N_RESERVAS} reservas.")
