-- O seed insere companies e users com id explícito sem avançar as sequences:
-- o próximo INSERT sem id tentava usar id=1 e falhava com duplicate key.
SELECT setval(pg_get_serial_sequence('companies', 'id'), coalesce((SELECT max(id) FROM companies), 0) + 1, false);
SELECT setval(pg_get_serial_sequence('users', 'id'), coalesce((SELECT max(id) FROM users), 0) + 1, false);
