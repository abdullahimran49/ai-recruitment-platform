"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { adminSession, apiGet, apiSend } from "@/lib/api";

export default function Manage() {
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [departments, setDepartments] = useState([]);
  const [admins, setAdmins] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [deptName, setDeptName] = useState("");
  const [form, setForm] = useState({
    name: "", email: "", password: "", department_id: "", role: "admin",
  });

  const reload = useCallback((s) => {
    apiGet("/api/admin/departments", s.token).then(setDepartments).catch((e) => setError(e.message));
    apiGet("/api/admin/users", s.token).then(setAdmins).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    const s = adminSession();
    if (!s) { router.push("/admin"); return; }
    if (s.role !== "super_admin") { router.push("/admin/dashboard"); return; }
    setSession(s);
    reload(s);
  }, [router, reload]);

  const addDept = async (e) => {
    e.preventDefault(); setError(""); setNotice("");
    try {
      await apiSend("/api/admin/departments", "POST", { name: deptName }, session.token);
      setDeptName(""); setNotice("Department created.");
      reload(session);
    } catch (err) { setError(err.message); }
  };

  const removeDept = async (id) => {
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/departments/${id}`, "DELETE", undefined, session.token);
      reload(session);
    } catch (err) { setError(err.message); }
  };

  const addAdmin = async (e) => {
    e.preventDefault(); setError(""); setNotice("");
    try {
      await apiSend("/api/admin/users", "POST", {
        ...form,
        department_id: form.role === "admin" ? Number(form.department_id) : null,
      }, session.token);
      setForm({ name: "", email: "", password: "", department_id: "", role: "admin" });
      setNotice("Admin created.");
      reload(session);
    } catch (err) { setError(err.message); }
  };

  const removeAdmin = async (uuid) => {
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/users/${uuid}`, "DELETE", undefined, session.token);
      reload(session);
    } catch (err) { setError(err.message); }
  };

  if (!session) return null;

  return (
    <main className="container">
      <div className="topbar">
        <div>
          <h1>Manage</h1>
          <p className="muted">Super admin — departments and admin accounts</p>
        </div>
        <Link href="/admin/dashboard"><button className="secondary">← Dashboard</button></Link>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      <div className="card">
        <h2>Departments</h2>
        <table>
          <tbody>
            {departments.map((d) => (
              <tr key={d.id}>
                <td>{d.name}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="danger" style={{ marginTop: 0 }}
                          onClick={() => removeDept(d.id)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <form onSubmit={addDept} className="row">
          <div>
            <label>New department name</label>
            <input value={deptName} required minLength={2}
                   onChange={(e) => setDeptName(e.target.value)} />
          </div>
          <div style={{ alignSelf: "end", flex: "0 0 auto" }}>
            <button>Add department</button>
          </div>
        </form>
      </div>

      <div className="card">
        <h2>Admins</h2>
        <table>
          <thead>
            <tr><th>Name</th><th>Email</th><th>Role</th><th>Department</th><th></th></tr>
          </thead>
          <tbody>
            {admins.map((a) => (
              <tr key={a.uuid}>
                <td>{a.name}</td>
                <td>{a.email}</td>
                <td>{a.role}</td>
                <td>{a.department || "all"}</td>
                <td style={{ textAlign: "right" }}>
                  <button className="danger" style={{ marginTop: 0 }}
                          onClick={() => removeAdmin(a.uuid)}>Delete</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <form onSubmit={addAdmin}>
          <div className="row">
            <div>
              <label>Name</label>
              <input value={form.name} required
                     onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <label>Email</label>
              <input type="email" value={form.email} required
                     onChange={(e) => setForm({ ...form, email: e.target.value })} />
            </div>
          </div>
          <div className="row">
            <div>
              <label>Password (min 8 chars)</label>
              <input type="password" value={form.password} required minLength={8}
                     onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </div>
            <div>
              <label>Role</label>
              <select value={form.role}
                      onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="admin">Department admin</option>
                <option value="super_admin">Super admin</option>
              </select>
            </div>
            <div>
              <label>Department</label>
              <select value={form.department_id} required={form.role === "admin"}
                      disabled={form.role !== "admin"}
                      onChange={(e) => setForm({ ...form, department_id: e.target.value })}>
                <option value="">— select —</option>
                {departments.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </select>
            </div>
          </div>
          <button>Create admin</button>
        </form>
      </div>
    </main>
  );
}
