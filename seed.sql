-- Run this against the RDS instance to create tables and seed fake listings
-- Connect via: psql -h <rds_endpoint> -U redline_admin -d redline

-- -------------------------------------------------------
-- TABLES
-- -------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    id          SERIAL PRIMARY KEY,
    make        VARCHAR(50) NOT NULL,
    model       VARCHAR(50) NOT NULL,
    year        INTEGER NOT NULL,
    price       NUMERIC(10, 2) NOT NULL,
    mileage     INTEGER NOT NULL,
    color       VARCHAR(30),
    description TEXT,
    image_url   VARCHAR(255),
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id           SERIAL PRIMARY KEY,
    cognito_sub  VARCHAR(255) UNIQUE NOT NULL,
    email        VARCHAR(255) NOT NULL,
    created_at   TIMESTAMP DEFAULT NOW()
);

-- -------------------------------------------------------
-- SEED LISTINGS
-- -------------------------------------------------------
INSERT INTO listings (make, model, year, price, mileage, color, description) VALUES
('Toyota',     'Camry',      2021, 24500.00,  28000, 'Silver',  'Well maintained, one owner, clean title'),
('Honda',      'Civic',      2020, 19800.00,  35000, 'Blue',    'Sport trim, sunroof, backup camera'),
('Ford',       'Mustang',    2019, 31000.00,  22000, 'Red',     'V8 GT, premium package, low miles'),
('Chevrolet',  'Silverado',  2022, 42000.00,  15000, 'Black',   'LTZ trim, tow package, crew cab'),
('BMW',        '3 Series',   2020, 38500.00,  30000, 'White',   '330i, M Sport package, heated seats'),
('Mercedes',   'C-Class',    2021, 41000.00,  20000, 'Gray',    'C300, AMG Line, panoramic roof'),
('Audi',       'A4',         2020, 36000.00,  27000, 'Navy',    'Premium Plus, quattro AWD, leather'),
('Tesla',      'Model 3',    2022, 44000.00,   8000, 'White',   'Long Range AWD, autopilot, low miles'),
('Hyundai',    'Elantra',    2021, 18500.00,  32000, 'Silver',  'SEL trim, apple carplay, warranty'),
('Kia',        'Telluride',  2021, 38000.00,  25000, 'Green',   'EX trim, 3rd row, AWD, one owner'),
('Subaru',     'Outback',    2022, 32000.00,  18000, 'Blue',    'Limited, eyesight safety, AWD'),
('Jeep',       'Wrangler',   2021, 39500.00,  20000, 'Orange',  'Unlimited Sport, 4x4, hardtop'),
('Mazda',      'CX-5',       2022, 29000.00,  16000, 'Red',     'Grand Touring, turbo, leather seats'),
('Volkswagen', 'Golf GTI',   2021, 28500.00,  22000, 'Black',   'Autobahn trim, manual, 2.0T'),
('Nissan',     'Altima',     2020, 20000.00,  38000, 'White',   'SR trim, FWD, heated seats, clean');
