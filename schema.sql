-- Esquema de base de datos: Cosesa - Seguimiento de Obras
PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS certificados;
DROP TABLE IF EXISTS usuario_obras;
DROP TABLE IF EXISTS personal_lavado;
DROP TABLE IF EXISTS piezas_lavado;
DROP TABLE IF EXISTS checklist_puntos;
DROP TABLE IF EXISTS checklist_lavado;
DROP TABLE IF EXISTS demoras;
DROP TABLE IF EXISTS fotos;
DROP TABLE IF EXISTS avance_log;
DROP TABLE IF EXISTS items;
DROP TABLE IF EXISTS obras;
DROP TABLE IF EXISTS usuarios;
DROP TABLE IF EXISTS clientes;

CREATE TABLE clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    cuit TEXT,
    contacto TEXT,
    creado_en TEXT DEFAULT (datetime('now'))
);

CREATE TABLE usuarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    rol TEXT NOT NULL CHECK (rol IN ('cosesa', 'cosesa_admin', 'cliente')),
    cliente_id INTEGER REFERENCES clientes(id) ON DELETE CASCADE,
    creado_en TEXT DEFAULT (datetime('now'))
);

CREATE TABLE obras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    nombre TEXT NOT NULL,
    descripcion TEXT,
    ubicacion TEXT,
    fecha_inicio TEXT,
    fecha_fin_estimada TEXT,
    estado TEXT NOT NULL DEFAULT 'en_curso' CHECK (estado IN ('planificada','en_curso','finalizada')),
    creado_en TEXT DEFAULT (datetime('now'))
);

-- Items = equipos / trabajos puntuales a intervenir dentro de una obra
CREATE TABLE items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    nombre TEXT NOT NULL,          -- ej: "Bomba centrífuga P-101"
    descripcion TEXT,
    tag_equipo TEXT,               -- identificador interno de equipo, opcional
    fecha_inicio TEXT,
    fecha_fin TEXT,
    fecha_inicio_plan TEXT,        -- cronograma planificado (antes de iniciar la obra)
    fecha_fin_plan TEXT,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','en_curso','finalizado')),
    avance_pct INTEGER NOT NULL DEFAULT 0,
    horas_hombre REAL NOT NULL DEFAULT 0,
    horas_maquina REAL NOT NULL DEFAULT 0,
    observaciones TEXT,
    creado_en TEXT DEFAULT (datetime('now'))
);

-- Bitácora histórica de avance por día (para el cronograma / seguimiento día a día)
CREATE TABLE avance_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL DEFAULT (date('now')),
    avance_pct INTEGER NOT NULL,
    comentario TEXT,
    horas_hombre_dia REAL NOT NULL DEFAULT 0,
    horas_maquina_dia REAL NOT NULL DEFAULT 0,
    usuario_id INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT (datetime('now'))
);

-- Demoras / tiempos muertos registrados durante la ejecución de un ítem
CREATE TABLE demoras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    fecha_inicio TEXT NOT NULL,
    fecha_fin TEXT NOT NULL,
    motivo TEXT,
    usuario_id INTEGER REFERENCES usuarios(id),
    creado_en TEXT DEFAULT (datetime('now'))
);

CREATE TABLE fotos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tipo TEXT NOT NULL CHECK (tipo IN ('antes','despues','progreso','certificado_lavado')),
    filename TEXT NOT NULL,
    comentario TEXT,
    subida_en TEXT DEFAULT (datetime('now')),
    usuario_id INTEGER REFERENCES usuarios(id)
);

-- Checklist de certificación de lavado hidrocinético, uno por ítem.
-- Genera un reporte en PDF con número único, para tildar a mano y firmar
-- (Responsable de Calidad COSESA y Responsable de Planta del cliente).
-- Los puntos del checklist en sí son una lista editable (ver checklist_puntos).
CREATE TABLE checklist_lavado (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
    numero_reporte TEXT UNIQUE,
    tipo_servicio TEXT NOT NULL DEFAULT 'Lavado hidrocinético industrial',
    actualizado_en TEXT DEFAULT (datetime('now')),
    usuario_id INTEGER REFERENCES usuarios(id)
);

-- Puntos individuales del checklist de un ítem (por defecto: Lavado, Secado).
-- Totalmente editable: se pueden renombrar, eliminar o agregar puntos nuevos.
CREATE TABLE checklist_puntos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    orden INTEGER NOT NULL DEFAULT 0,
    etiqueta TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','ok','na')),
    creado_en TEXT DEFAULT (datetime('now'))
);

-- Piezas / subitems de un equipo con tubos (ej: intercambiador de calor):
-- tapa, tapa olla, distribuidor, aros de flotante, cabezal flotante, u otras piezas cargadas a mano.
-- Cada pieza se certifica por separado (a veces se lavan en días distintos) y lleva
-- espacio para dos firmas: Responsable de Calidad COSESA y Responsable de Planta del cliente.
CREATE TABLE piezas_lavado (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    nombre TEXT NOT NULL,
    fecha_recepcion TEXT,
    fecha_lavado TEXT,
    precinto TEXT,
    confirmado TEXT NOT NULL DEFAULT 'pendiente' CHECK (confirmado IN ('pendiente','ok')),
    firmado_por_cosesa TEXT,
    firmado_por_cliente TEXT,
    creado_en TEXT DEFAULT (datetime('now'))
);

-- Personal asignado a un ítem, por turno (día / noche), y cantidad de bombas
-- usadas en ese turno. Solo cantidades (sin nombres individuales). Se usa para
-- calcular horas-hombre / horas-máquina junto con las horas de trabajo netas
-- (horas totales del ítem menos las demoras registradas).
CREATE TABLE personal_lavado (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    turno TEXT NOT NULL CHECK (turno IN ('dia','noche')),
    supervisores INTEGER NOT NULL DEFAULT 0,
    tecnicos_hs INTEGER NOT NULL DEFAULT 0,
    punteros INTEGER NOT NULL DEFAULT 0,
    operarios INTEGER NOT NULL DEFAULT 0,
    bombas INTEGER NOT NULL DEFAULT 0,
    actualizado_en TEXT DEFAULT (datetime('now')),
    UNIQUE(item_id, turno)
);

-- Asignación de obras visibles para cada usuario tipo "cliente". Un usuario
-- cliente solo puede ver (dashboard, reportes, PDFs) las obras que aparecen
-- acá, sin importar cuántas obras tenga cargadas el cliente (empresa) al que
-- pertenece. Se administra desde /cosesa/usuarios.
CREATE TABLE usuario_obras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    UNIQUE(usuario_id, obra_id)
);

-- Tablas de certificación / actas firmadas por el cliente, adjuntadas por obra.
-- El cliente puede descargar una copia desde su informe.
CREATE TABLE certificados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obra_id INTEGER NOT NULL REFERENCES obras(id) ON DELETE CASCADE,
    nombre_original TEXT NOT NULL,
    filename TEXT NOT NULL,
    subida_en TEXT DEFAULT (datetime('now')),
    usuario_id INTEGER REFERENCES usuarios(id)
);
