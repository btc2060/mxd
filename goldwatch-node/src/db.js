import mysql from 'mysql2/promise';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { ROOT } from './config.js';

export const dateSQL = value => new Date(value).toISOString().slice(0, 23).replace('T', ' ');
export const iso = value => {
  if (value == null) return null;
  const text = value instanceof Date ? value : String(value).replace(' ', 'T');
  return new Date(value instanceof Date || /Z$|[+-]\d\d:\d\d$/.test(text) ? text : text + 'Z').toISOString();
};
export const millis = value => value == null ? null : Date.parse(iso(value));

export async function database(config) {
  const pool = mysql.createPool({ ...config.mysql, charset: 'utf8mb4', timezone: 'Z', dateStrings: true,
    connectionLimit: 5, waitForConnections: true, queueLimit: 30, connectTimeout: 10000, enableKeepAlive: true,
    multipleStatements: false });
  try {
    const schema = readFileSync(path.join(ROOT, 'src/schema.sql'), 'utf8');
    for (const command of schema.split(';').map(s => s.trim()).filter(Boolean)) await pool.query(command);
    await pool.query(`INSERT IGNORE INTO preferences
      (id, interval_seconds, enabled, max_pages, engine, merchant_selector, player_selector, next_due_at, last_manual_at, requested, failure_streak)
      VALUES (1,600,1,1,'auto','','',NULL,NULL,0,0)`);
    await pool.query("INSERT IGNORE INTO collector_lease (id,owner,expires_at) VALUES (1,'',NULL)");
    return pool;
  } catch (error) { await pool.end(); throw error; }
}

export async function transaction(pool, action) {
  const connection = await pool.getConnection();
  try { await connection.beginTransaction(); const result = await action(connection); await connection.commit(); return result; }
  catch (error) { await connection.rollback(); throw error; }
  finally { connection.release(); }
}
