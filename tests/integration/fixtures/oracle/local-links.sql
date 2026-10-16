-- SQL*Plus, local DBMCP_TEST only. Remote DB must already have common.sql.
-- Do not spool this session. Hidden input is used only by disposable DBA setup.
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET VERIFY OFF
ACCEPT remote_endpoint CHAR PROMPT 'Disposable remote host:port/service: '
ACCEPT remote_password CHAR PROMPT 'Disposable remote DBMCP_TEST password: ' HIDE
CREATE DATABASE LINK DBMCP.REMOTE.TEST CONNECT TO DBMCP_TEST IDENTIFIED BY "&remote_password" USING '&remote_endpoint';
UNDEFINE remote_password
UNDEFINE remote_endpoint
CREATE SYNONYM DBMCP_REMOTE_ORDERS FOR DBMCP_TEST.DBMCP_ORDERS@DBMCP.REMOTE.TEST;
CREATE SYNONYM DBMCP_REMOTE_CHAIN FOR DBMCP_REMOTE_ORDERS;
