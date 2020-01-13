from katana.api.mongoUtils import mongoUtils
from katana.api.osmUtils import osmUtils
import pickle
import time
import logging

# Logging Parameters
logger = logging.getLogger(__name__)
file_handler = logging.handlers.RotatingFileHandler(
    'katana.log', maxBytes=10000, backupCount=5)
stream_handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s %(name)s %(levelname)s %(message)s')
stream_formatter = logging.Formatter(
    '%(asctime)s %(name)s %(levelname)s %(message)s')
file_handler.setFormatter(formatter)
stream_handler.setFormatter(stream_formatter)
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)


NEST_KEYS_OBJ = ("sst_id", "shared", "network_DL_throughput",
                 "ue_DL_throughput", "network_UL_throughput",
                 "ue_UL_throughput", "group_communication_support", "mtu",
                 "number_of_terminals", "positional_support",
                 "device_velocity", "terminal_density")

NEST_KEYS_LIST = ("coverage", "ns_list", "radio_spectrum", "probe_list")


def do_work(nest_req):
    """
    Creates the network slice
    """

    # Recreate the NEST with None options where missiong
    nest = {"_id": nest_req["_id"], "created_at": nest_req["created_at"],
            "deployment_time": {"Placement_Time": None,
                                "Provisioning_Time": None,
                                "WAN_Deployment_Time": None,
                                "NS_Deployment_Time": None,
                                "Radio_Configuration_Time": None,
                                "Slice_Deployment_Time": None}}
    for nest_key in NEST_KEYS_OBJ:
        nest[nest_key] = nest_req.get(nest_key, None)
    for nest_key in NEST_KEYS_LIST:
        nest[nest_key] = nest_req.get(nest_key, [])

    # **** STEP-1: Placement ****
    nest['status'] = 'Placement'
    nest["phase"] = 0
    mongoUtils.update("slice", nest['_id'], nest)
    logger.info("Status: Placement")
    placement_start_time = time.time()

    # Find the supported sst based on the sst id generated by the slice mapping
    sst = mongoUtils.get("sst", nest["sst_id"])
    if not sst:
        logger.error("No SST provided - Terminating Slice")
        delete_slice(nest)
        return

    # Make the NS and PNF list
    # Initiate the lists
    ns_list = sst.get("ns_list", []) + nest.get("ns_list", [])
    pnf_list = sst.get("pnf_list", [])
    vim_list = []
    vim_dict = {}
    pdu_list = []
    ems_messages = {}
    # Get the NSs and PNFsfrom the supported sst
    for ns in ns_list:
        if not ns["placement"]:
            ns["placement"] = [{"location": "core"}]
        else:
            ns["placement"] = []
            for location in nest["coverage"]:
                ns["placement"].append({"location": location})

    for pnf in pnf_list:
        pdu = mongoUtils.find("pdu", {"id": pnf["pdu-id"]})
        if not pdu:
            # ERROR HANDLING: The ns is not optional and the nsd is not
            # on the NFVO - stop and return
            delete_slice(nest)
            logger.error(f"PDU {pnf['pdu-id']} not found")
            return
        pdu_list.append(pdu["id"])
        pdu["tenants"].append(nest["_id"])
        mongoUtils.update("pdu", pdu["_id"], pdu)
        ems = pnf.get("ems-id", None)
        if ems:
            ems_messages[ems] = ems_messages.get(ems, {"conf_ns_list": [],
                                                       "conf_pnf_list": []})
            ems_messages[ems]["conf_pnf_list"].append(
                {"name": pnf["pnf-name"], "ip": pdu["ip"],
                 "pdu-location": pdu["location"]})
    nest["pdu_list"] = pdu_list

    # Find the details for each NS
    pop_list = []
    for ns in ns_list:
        # Search the nsd collection in Mongo for the nsd
        nsd = mongoUtils.find("nsd", {"id": ns["nsd-id"],
                              "nfvo_id": ns["nfvo-id"]})
        if not nsd:
            # Bootstrap the NFVO to check for NSDs that are not in mongo
            # If again is not found, check if NS is optional.
            # If it is just remove it, else error
            nfvo_obj_json = mongoUtils.find("nfvo_obj", {"id": ns["nfvo-id"]})
            if not nfvo_obj_json:
                # ERROR HANDLING: There is no OSM for that ns - stop and return
                logger.error("There is no NFVO with id {}"
                             .format(ns["nfvo-id"]))
                delete_slice(nest)
                return
            nfvo = pickle.loads(nfvo_obj_json["obj"])
            osmUtils.bootstrapNfvo(nfvo)
            nsd = mongoUtils.find("nsd", {"id": ns["nsd-id"],
                                  "nfvo_id": ns["nfvo-id"]})
            if not nsd and ns.get("optional", False):
                pop_list.append(ns)
            else:
                # ERROR HANDLING: The ns is not optional and the nsd is not
                # on the NFVO - stop and return
                delete_slice(nest)
                logger.error(f"NSD {ns['nsd-id']} not found on \
OSM {ns['nfvo-id']}")
                return
        nsd = mongoUtils.find("nsd", {"id": ns["nsd-id"]})
        ns["nsd-info"] = nsd
    ns_list = [ns for ns in ns_list if ns not in pop_list]

    # Select the VIMs for each NS acording to location
    for ns in ns_list:
        ns["vims"] = []
        for site in ns["placement"]:
            get_vim = list(mongoUtils.find_all('vim', {"location":
                           site["location"]}))
            if not get_vim:
                # ERROR HANDLING: There is no VIM at that location
                logger.error("VIM not found")
                delete_slice(nest)
                return
            # TODO: Check the available resources and select vim
            # Temporary use the first element
            selected_vim = get_vim[0]["id"]
            ns["vims"].append(selected_vim)
            try:
                vim_dict[selected_vim]["ns_list"].append(ns["ns-name"])
                if ns["nfvo-id"] not in vim_dict[selected_vim]["nfvo_list"]:
                    vim_dict[selected_vim]["nfvo_list"].append(ns["nfvo-id"])
            except KeyError:
                vim_dict[selected_vim] = {"ns_list": [ns["ns-name"]],
                                          "nfvo_list": [ns["nfvo-id"]]}
            site["vim"] = selected_vim
            if selected_vim not in vim_list:
                vim_list.append(selected_vim)

    # Create the information for the EMS, WIM MON
    end_points = {"vims": vim_list, "pdus": pdu_list,
                  "probes": nest["probe_list"]}
    wim_data = {"network_DL_throughput": nest["network_DL_throughput"],
                "network_UL_throughput": nest["network_UL_throughput"],
                "mtu": nest["mtu"], "end_points": end_points}

    nest["network functions"] = {"ns_list": ns_list, "pnf_list": pnf_list}
    nest["vim_list"] = vim_dict
    nest['deployment_time']['Placement_Time'] = format(
        time.time() - placement_start_time, '.4f')

    # **** STEP-2: Resource Provisioning ****
    nest['status'] = 'Provisioning'
    mongoUtils.update("slice", nest['_id'], nest)
    logger.info("Status: Provisioning")
    prov_start_time = time.time()

    # *** STEP-2a: Cloud ***
    # *** STEP-2a-i: Create the new tenant/project on the VIM ***
    for num, (vim, vim_info) in enumerate(vim_dict.items()):
        target_vim = mongoUtils.find("vim", {"id": vim})
        target_vim_obj = pickle.loads(
            mongoUtils.find("vim_obj", {"id": vim})["obj"])
        # Define project parameters
        tenant_project_name = 'vim_{0}_katana_{1}'.format(
            num, nest['_id'])
        tenant_project_description = 'vim_{0}_katana_{1}'.format(
            num, nest['_id'])
        tenant_project_user = 'vim_{0}_katana_{1}'.format(
            num, nest['_id'])
        tenant_project_password = 'password'
        ids = target_vim_obj.create_slice_prerequisites(
            tenant_project_name,
            tenant_project_description,
            tenant_project_user,
            tenant_project_password,
            nest['_id']
        )
        # Register the tenant to the mongo db
        target_vim["tenants"][nest["_id"]] = tenant_project_name
        mongoUtils.update("vim", target_vim["_id"], target_vim)

        # STEP-2a-ii: Αdd the new VIM tenant to NFVO
        if target_vim["type"] == "openstack":
            # Update the config parameter for the tenant
            config_param = dict(security_groups=ids["secGroupName"])
        elif target_vim["type"] == "opennebula":
            config_param = selected_vim['config']

        for nfvo_id in vim_info["nfvo_list"]:
            target_nfvo = mongoUtils.find("nfvo", {"id": nfvo_id})
            target_nfvo_obj = pickle.loads(
                mongoUtils.find("nfvo_obj", {"id": nfvo_id})["obj"])
            vim_id = target_nfvo_obj.addVim(
                tenant_project_name, target_vim['password'],
                target_vim['type'], target_vim['auth_url'],
                target_vim['username'],
                config_param)
            vim_info["nfvo_vim_account"] = vim_info.get("nfvo_vim_account", {})
            vim_info["nfvo_vim_account"][nfvo_id] = vim_id
            # Register the tenant to the mongo db
            target_nfvo["tenants"][nest["_id"]] =\
                target_nfvo["tenants"].get(nest["_id"], [])
            target_nfvo["tenants"][nest["_id"]].append(vim_id)
            mongoUtils.update("nfvo", target_nfvo["_id"], target_nfvo)

    nest['phase'] = 1
    mongoUtils.update("slice", nest['_id'], nest)
    # *** STEP-2b: WAN ***
    if (mongoUtils.count('wim') <= 0):
        logger.warning('There is no registered WIM')
    else:
        wan_start_time = time.time()
        # Select WIM - Assume that there is only one registered
        wim_list = list(mongoUtils.index('wim'))
        target_wim = wim_list[0]
        target_wim_id = target_wim["id"]
        target_wim_obj = pickle.loads(
            mongoUtils.find("wim_obj", {"id": target_wim_id})["obj"])
        target_wim_obj.create_slice(wim_data)
        nest["wim_data"] = wim_data
        target_wim["slices"][nest["_id"]] = end_points
        mongoUtils.update("wim", target_wim["_id"], target_wim)
        nest['deployment_time']['WAN_Deployment_Time'] =\
            format(time.time() - wan_start_time, '.4f')
    nest['deployment_time']['Provisioning_Time'] =\
        format(time.time() - prov_start_time, '.4f')

    # **** STEP-3: Slice Activation Phase****
    nest['status'] = 'Activation'
    nest['phase'] = 2
    mongoUtils.update("slice", nest['_id'], nest)
    logger.info("Status: Activation")
    # *** STEP-3a: Cloud ***
    # Instantiate NS
    nest['deployment_time']['NS_Deployment_Time'] = {}
    for ns in ns_list:
        ns_start_time = time.time()
        target_nfvo = mongoUtils.find("nfvo", {"id": ns["nfvo-id"]})
        target_nfvo_obj = pickle.loads(
            mongoUtils.find("nfvo_obj", {"id": ns["nfvo-id"]})["obj"])
        for site in ns["placement"]:
            selected_vim = site["vim"]
            nfvo_vim_account = \
                vim_dict[selected_vim]["nfvo_vim_account"][ns["nfvo-id"]]
            nfvo_inst_ns = target_nfvo_obj.instantiateNs(
                ns["ns-name"],
                ns["nsd-id"],
                nfvo_vim_account
            )
            site["nfvo_inst_ns"] = nfvo_inst_ns
            time.sleep(4)
        time.sleep(2)

    # Get the nsr for each service and wait for the activation
    for ns in ns_list:
        target_nfvo = mongoUtils.find("nfvo", {"id": ns["nfvo-id"]})
        target_nfvo_obj = pickle.loads(
            mongoUtils.find("nfvo_obj", {"id": ns["nfvo-id"]})["obj"])
        for site in ns["placement"]:
            insr = target_nfvo_obj.getNsr(site["nfvo_inst_ns"])
            while (insr["operational-status"] != "running" or
                   insr["config-status"] != "configured"):
                time.sleep(10)
                insr = target_nfvo_obj.getNsr(site["nfvo_inst_ns"])
            nest['deployment_time']['NS_Deployment_Time'][ns['ns-name']] =\
                format(time.time() - ns_start_time, '.4f')
            # Get the IPs of the instantiated NS
            site["vnfs"] = []
            vnfr_id_list = target_nfvo_obj.getVnfrId(insr)
            for ivnfr_id in vnfr_id_list:
                vnfr = target_nfvo_obj.getVnfr(ivnfr_id)
                site["vnfs"].append(target_nfvo_obj.getIPs(vnfr))

    nest['phase'] = 3
    mongoUtils.update("slice", nest['_id'], nest)
    # *** STEP-3b: Radio Slice Configuration ***
    if (mongoUtils.count('ems') <= 0):
        logger.warning('There is no registered EMS')
    else:
        # Add the management IPs for the NS sent ems in ems_messages:
        radio_start_time = time.time()
        for ns in ns_list:
            try:
                ems = ns["ems-id"]
            except KeyError:
                continue
            else:
                ems_messages[ems] = ems_messages.get(
                    ems, {"conf_ns_list": [], "conf_pnf_list": []})
                for site in ns["placement"]:
                    data = {"name": ns["ns-name"],
                            "location": site["location"],
                            "vnf_list": site["vnfs"]}
                    ems_messages[ems]["conf_ns_list"].append(data)
        # Create and send the messages
        ems_data = {"ue_DL_throughput": nest["ue_DL_throughput"],
                    "ue_UL_throughput": nest["ue_UL_throughput"],
                    "group_communication_support":
                    nest["group_communication_support"],
                    "number_of_terminals": nest["number_of_terminals"],
                    "positional_support": nest["positional_support"],
                    "radio_spectrum": nest["radio_spectrum"],
                    "device_velocity": nest["device_velocity"],
                    "terminal_density": nest["terminal_density"]}
        for ems_id, ems_message in ems_messages.items():
            # Find the EMS
            target_ems = mongoUtils.find("ems", {"id": ems_id})
            if not target_ems:
                # ERROR HANDLING: There is no such EMS
                logger.error("EMS {} not found - No configuration".
                             format(ems_id))
                continue
            target_ems_obj = pickle.loads(
                mongoUtils.find("ems_obj", {"id": ems_id})["obj"])
            # Create the message
            ems_message.update(ems_data)
            # Send the message
            target_ems_obj.conf_radio(ems_message)
        nest["ems_data"] = ems_messages
        nest['deployment_time']['Radio_Configuration_Time']\
            = format(time.time() - radio_start_time, '.4f')

    # *** STEP-4: Finalize ***
    logger.info("Status: Running")
    nest['status'] = 'Running'
    nest['phase'] = 4
    nest['deployment_time']['Slice_Deployment_Time'] =\
        format(time.time() - nest['created_at'], '.4f')
    mongoUtils.update("slice", nest['_id'], nest)


def delete_slice(slice_json):
    """
    Deletes the given network slice
    """

    # Update the slice status in mongo db
    slice_json["status"] = "Terminating"
    mongoUtils.update("slice", slice_json['_id'], slice_json)
    logger.info("Status: Terminating")

    # *** Step-1: Radio Slice Configuration ***
    if slice_json['phase'] >= 4:
        ems_messages = slice_json.get("ems_data", None)
        if ems_messages:
            for ems_id, ems_message in ems_messages.items():
                # Find the EMS
                target_ems = mongoUtils.find("ems", {"id": ems_id})
                if not target_ems:
                    # ERROR HANDLING: There is no such EMS
                    logger.error("EMS {} not found - No configuration".
                                 format(ems_id))
                    continue
                target_ems_obj = pickle.loads(
                    mongoUtils.find("ems_obj", {"id": ems_id})["obj"])
                target_ems_obj.del_slice(ems_message)
    else:
        logger.warning("Something went wrong with EMS configuration during \
slice creation")

    # Release PDUs
    try:
        for ipdu in slice_json["pdu_list"]:
            pdu = mongoUtils.find("pdu", {"id": ipdu})
            pdu["tenants"].remove(slice_json["_id"])
            mongoUtils.update("pdu", pdu["_id"], pdu)
    except (KeyError, ValueError):
        logger.info("No PDU on the slice")

    # *** Step-2: WAN Slice ***
    wim_data = slice_json.get("wim_data", None)
    if wim_data:
        # Select WIM - Assume that there is only one registered
        wim_list = list(mongoUtils.index('wim'))
        if wim_list:
            target_wim = wim_list[0]
            target_wim_id = target_wim["id"]
            target_wim_obj = pickle.loads(
                mongoUtils.find("wim_obj", {"id": target_wim_id})["obj"])
            target_wim_obj.del_slice(wim_data)
            del target_wim["slices"][slice_json["_id"]]
            mongoUtils.update("wim", target_wim["_id"], target_wim)
        else:
            logger.warning("Cannot find WIM - WAN Slice will not be deleted")

    # *** Step-3: Cloud ***
    nf = slice_json.get("network functions", None)
    ns_list = nf["ns_list"]
    try:
        vim_error_list = []
        for ns in ns_list:
            # Get the NFVO
            nfvo_id = ns["nfvo-id"]
            target_nfvo = mongoUtils.find("nfvo", {"id": ns["nfvo-id"]})
            if not target_nfvo:
                logger.warning(
                    "NFVO with id {} was not found - NSs won't terminate"
                    .format(nfvo_id))
                vim_error_list += ns["vims"]
                continue
            target_nfvo_obj = pickle.loads(
                mongoUtils.find("nfvo_obj", {"id": ns["nfvo-id"]})["obj"])
            # Stop the NS
            for site in ns["placement"]:
                target_nfvo_obj.deleteNs(site["nfvo_inst_ns"])
                while True:
                    if target_nfvo_obj.checkNsLife(site["nfvo_inst_ns"]):
                        break
                    time.sleep(5)
    except KeyError as e:
        logger.warning(
            f"Error, not all NSs started or terminated correctly {e}")

    try:
        vim_dict = slice_json["vim_list"]
        for vim, vim_info in vim_dict.items():
            # Delete the new tenants from the NFVO
            for nfvo, vim_account in vim_info["nfvo_vim_account"].items():
                # Get the NFVO
                target_nfvo = mongoUtils.find("nfvo", {"id": nfvo})
                target_nfvo_obj = pickle.loads(
                    mongoUtils.find("nfvo_obj", {"id": nfvo})["obj"])
                # Delete the VIM and update nfvo db
                target_nfvo_obj.deleteVim(vim_account)
                target_nfvo["tenants"][slice_json["_id"]].remove(vim_account)
                if len(target_nfvo["tenants"][slice_json["_id"]]) == 0:
                    del target_nfvo["tenants"][slice_json["_id"]]
                mongoUtils.update("nfvo", target_nfvo["_id"], target_nfvo)
            # Delete the tenants from every vim
            if vim not in vim_error_list:
                # Get the VIM
                target_vim = mongoUtils.find("vim", {"id": vim})
                if not target_vim:
                    logger.warning(
                        "VIM id {} was not found - Tenant won't be deleted"
                        .format(vim))
                    continue
                target_vim_obj = pickle.loads(
                    mongoUtils.find("vim_obj", {"id": vim})["obj"])
                target_vim_obj.delete_proj_user(
                    target_vim["tenants"][slice_json["_id"]])
                del target_vim["tenants"][slice_json["_id"]]
                mongoUtils.update("vim", target_vim["_id"], target_vim)
    except KeyError as e:
        logger.warning(
            f"Error, not all tenants created or removed correctly {e}")

    mongoUtils.delete("slice", slice_json["_id"])